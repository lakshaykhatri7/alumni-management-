from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, session
import os
import hashlib
import json
import logging
import re
import secrets
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import random
import razorpay
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
import ai_features as ai
import reports
import db
from db import get_db, init_db, IntegrityError
from email_alerts import configure_mail, mail_configured, send_bulk_email, send_single_email

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "alumni_secret_key_change_me")
configure_mail(app)

# used to send delayed emails (e.g. donation thank-you 2 minutes after the donation is recorded)
scheduler = BackgroundScheduler()
scheduler.start()

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)) if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET else None

# only alumni who have actually graduated (passed out) can register
MIN_PASSOUT_YEAR = 1950
MAX_PASSOUT_YEAR = datetime.now().year - 1

# profile photo upload is optional - alumni can skip it entirely
PHOTO_UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads", "photos")
ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_PHOTO_SIZE_BYTES = 3 * 1024 * 1024
os.makedirs(PHOTO_UPLOAD_FOLDER, exist_ok=True)


def _allowed_photo(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_PHOTO_EXTENSIONS


def save_profile_photo(file_storage, alumni_id, old_photo_path):
    """Saves an uploaded photo and returns its relative static path, or None if no file given."""
    if not file_storage or not file_storage.filename:
        return None
    if not _allowed_photo(file_storage.filename):
        flash("Photo must be a PNG, JPG, GIF or WEBP file.", "danger")
        return None
    file_storage.seek(0, os.SEEK_END)
    size = file_storage.tell()
    file_storage.seek(0)
    if size > MAX_PHOTO_SIZE_BYTES:
        flash("Photo is too large (max 3MB).", "danger")
        return None
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    filename = secure_filename(f"alumni_{alumni_id}_{secrets.token_hex(8)}.{ext}")
    file_storage.save(os.path.join(PHOTO_UPLOAD_FOLDER, filename))
    if old_photo_path:
        old_full_path = os.path.join(app.root_path, "static", old_photo_path.lstrip("/"))
        if os.path.isfile(old_full_path):
            try:
                os.remove(old_full_path)
            except OSError:
                pass
    return f"uploads/photos/{filename}"


def hash_password(password):
    return hashlib.sha256((password or "").strip().encode("utf-8")).hexdigest()


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def is_valid_email(email):
    return bool(email) and bool(EMAIL_RE.match(email.strip()))


def login_required(func):
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    wrapper.__name__ = func.__name__
    return wrapper


def admin_required(func):
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return func(*args, **kwargs)

    wrapper.__name__ = func.__name__
    return wrapper


@app.before_request
def enforce_login():
    public_paths = {"login", "logout", "signup", "static", "forgot_password", "reset_password", "healthz"}
    endpoint = request.endpoint or ""
    if endpoint in public_paths:
        return
    if not session.get("user_id"):
        return redirect(url_for("login"))
    # re-check suspension in real time in case admin revoked access mid-session
    conn = get_db()
    user = conn.execute("SELECT is_active FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    if not user or not user.get("is_active", True):
        session.clear()
        flash("This account has been suspended. Contact the admin.", "danger")
        return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        batch_year_raw = request.form.get("batch_year", "").strip()

        if not name or not email or not password:
            flash("Name, email and password are required.", "danger")
            return render_template("signup.html", min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR, form=request.form)
        if not is_valid_email(email):
            flash("Enter a valid email address (e.g. name@example.com).", "danger")
            return render_template("signup.html", min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR, form=request.form)
        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("signup.html", min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR, form=request.form)

        try:
            batch_year = int(batch_year_raw)
        except ValueError:
            batch_year = None
        if not batch_year or not (MIN_PASSOUT_YEAR <= batch_year <= MAX_PASSOUT_YEAR):
            flash(f"Enter a valid passout year between {MIN_PASSOUT_YEAR} and {MAX_PASSOUT_YEAR}. Current students cannot register.", "danger")
            return render_template("signup.html", min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR, form=request.form)

        conn = get_db()
        try:
            existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                flash("An account with this email already exists. Please log in.", "warning")
                conn.close()
                return redirect(url_for("login"))

            conn.execute(
                """INSERT INTO alumni (name,email,phone,batch_year,department,current_company,designation,city,linkedin)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (name, email, request.form.get("phone"), batch_year, request.form.get("department"),
                 request.form.get("current_company"), request.form.get("designation"),
                 request.form.get("city"), request.form.get("linkedin")),
            )
            alumni_row = conn.execute("SELECT id FROM alumni WHERE email = ?", (email,)).fetchone()
            conn.execute(
                "INSERT INTO users (alumni_id, email, password_hash, role) VALUES (?, ?, ?, 'alumni')",
                (alumni_row["id"], email, hash_password(password)),
            )
            conn.commit()
            flash("Account created successfully. You can log in now.", "success")
            return redirect(url_for("login"))
        except IntegrityError:
            conn.close()
            flash("An account with this email already exists.", "danger")
            return render_template("signup.html", min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR, form=request.form)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return render_template("signup.html", min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR, form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        login_role = request.form.get("login_role", "alumni")
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        if user and not user.get("is_active", True):
            flash("This account has been suspended. Contact the admin.", "danger")
            return render_template("login.html")
        if user and user["password_hash"] == hash_password(password):
            if user["role"] != login_role:
                flash(f"This is a {user['role']} account. Switch the toggle to {user['role']} and log in again.", "warning")
                return render_template("login.html")
            session.clear()
            session["user_id"] = user["id"]
            session["email"] = user["email"]
            session["role"] = user["role"]
            session["alumni_id"] = user["alumni_id"]
            flash("Login successful.", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


RESET_TOKEN_TTL_MINUTES = 30


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        conn = get_db()
        user = conn.execute("SELECT id, email FROM users WHERE email = ?", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
            conn.execute(
                "UPDATE users SET reset_token_hash = ?, reset_token_expires = ? WHERE id = ?",
                (token_hash, expires, user["id"]),
            )
            conn.commit()
            reset_link = url_for("reset_password", token=token, _external=True)
            body = (
                f"We received a request to reset your AlumniMS password.\n\n"
                f"Reset link (valid for {RESET_TOKEN_TTL_MINUTES} minutes):\n{reset_link}\n\n"
                f"If you did not request this, you can safely ignore this email."
            )
            emailed = send_single_email("Reset your AlumniMS password", body, user["email"])
            if not emailed and not mail_configured():
                # dev fallback so the flow is testable without email configured
                app.logger.info(f"Password reset link for {email}: {reset_link}")
        conn.close()
        # always show the same message so we don't reveal whether an email is registered
        flash("If that email is registered, a password reset link has been sent.", "info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    conn = get_db()
    user = conn.execute(
        "SELECT id, reset_token_expires FROM users WHERE reset_token_hash = ?", (token_hash,)
    ).fetchone()
    if not user or not user["reset_token_expires"] or user["reset_token_expires"] < datetime.now(timezone.utc).replace(tzinfo=None):
        conn.close()
        flash("This reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not password or password != confirm_password:
            conn.close()
            flash("Passwords do not match.", "danger")
            return render_template("reset_password.html", token=token)
        conn.execute(
            "UPDATE users SET password_hash = ?, reset_token_hash = NULL, reset_token_expires = NULL WHERE id = ?",
            (hash_password(password), user["id"]),
        )
        conn.commit()
        conn.close()
        flash("Password reset successful. You can log in now.", "success")
        return redirect(url_for("login"))

    conn.close()
    return render_template("reset_password.html", token=token)

# ---------- Logging: writes structured JSON lines to logs/app.log (rotates at 1MB, keeps 5 backups) ----------
class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_file_handler = RotatingFileHandler(os.path.join(LOG_DIR, "app.log"), maxBytes=1_000_000, backupCount=5)
_file_handler.setFormatter(JsonFormatter())
_file_handler.setLevel(logging.INFO)
app.logger.addHandler(_file_handler)
app.logger.setLevel(logging.INFO)
logging.getLogger("werkzeug").addHandler(_file_handler)


@app.route("/healthz")
def healthz():
    """Health check for Render/uptime monitors - verifies the app can actually reach the database."""
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        app.logger.exception("Health check failed")
        return jsonify({"status": "error", "detail": str(e)}), 503


def build_network_matches(source_alumni, all_alumni, limit=4):
    """Recommend strong alumni connections for networking based on shared profile traits."""
    if not source_alumni or not all_alumni:
        return []

    def normalize(value):
        return (value or "").strip().lower()

    matches = []
    for alum in all_alumni:
        if alum.get("id") == source_alumni.get("id"):
            continue

        score = 0
        reasons = []

        if str(alum.get("batch_year") or "") and str(alum.get("batch_year")) == str(source_alumni.get("batch_year") or ""):
            score += 6
            reasons.append("same batch")
        if normalize(alum.get("department")) and normalize(alum.get("department")) == normalize(source_alumni.get("department")):
            score += 5
            reasons.append("same department")
        if normalize(alum.get("current_company")) and normalize(alum.get("current_company")) == normalize(source_alumni.get("current_company")):
            score += 4
            reasons.append("same company")
        if normalize(alum.get("city")) and normalize(alum.get("city")) == normalize(source_alumni.get("city")):
            score += 3
            reasons.append("same city")
        if normalize(alum.get("designation")) and normalize(alum.get("designation")) == normalize(source_alumni.get("designation")):
            score += 2
            reasons.append("similar role")

        if not reasons:
            continue

        matches.append({
            "id": alum.get("id"),
            "name": alum.get("name"),
            "email": alum.get("email"),
            "batch_year": alum.get("batch_year"),
            "department": alum.get("department"),
            "current_company": alum.get("current_company"),
            "city": alum.get("city"),
            "score": score,
            "reason": ", ".join(reasons[:3]),
        })

    matches.sort(key=lambda item: (-item["score"], item["name"]))
    return matches[:limit]


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    app.logger.exception("Unhandled exception on %s %s", request.method, request.path)
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    if request.path.startswith("/ai/") or request.is_json:
        return jsonify({"error": f"Server error: {e}"}), 500
    return render_template("dashboard.html", stats={"alumni": 0, "events": 0, "donations": 0, "jobs": 0},
                            recent_alumni=[], upcoming_events=[]), 500


# ---------- DASHBOARD ----------
@app.route("/")
@login_required
def dashboard():
    is_admin = session.get("role") == "admin"
    conn = get_db()
    upcoming_events = conn.execute("SELECT * FROM events ORDER BY event_date ASC LIMIT 5").fetchall()
    recent_jobs = conn.execute(
        """SELECT j.*, a.name posted_by_name FROM jobs j
           LEFT JOIN alumni a ON a.id=j.posted_by_id ORDER BY posted_at DESC LIMIT 5"""
    ).fetchall()

    stats = None
    recent_alumni = []
    all_alumni = []
    spotlight = None

    if is_admin:
        stats = {
            "alumni": conn.execute("SELECT COUNT(*) c FROM alumni").fetchone()["c"],
            "events": conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
            "donations": conn.execute("SELECT COALESCE(SUM(amount),0) s FROM donations").fetchone()["s"],
            "jobs": conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"],
        }
        recent_alumni = conn.execute("SELECT * FROM alumni ORDER BY id DESC LIMIT 5").fetchall()
        all_alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
        spotlight = recent_alumni[0] if recent_alumni else None
    elif session.get("alumni_id"):
        all_alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
        spotlight = conn.execute("SELECT * FROM alumni WHERE id = ?", (session.get("alumni_id"),)).fetchone()

    conn.close()

    smart_connections = build_network_matches(spotlight, all_alumni, limit=4) if spotlight else []

    return render_template(
        "dashboard.html",
        stats=stats,
        recent_alumni=recent_alumni,
        upcoming_events=upcoming_events,
        recent_jobs=recent_jobs,
        smart_connections=smart_connections,
        spotlight_name=spotlight["name"] if spotlight else "Alumni",
        current_user=session.get("email"),
        user_role=session.get("role"),
        is_admin=is_admin,
    )


# ---------- ALUMNI CRUD ----------
@app.route("/alumni")
@login_required
@admin_required
def alumni_list():
    q = request.args.get("q", "").strip()
    batch = request.args.get("batch", "").strip()
    dept = request.args.get("dept", "").strip()
    smart = request.args.get("smart", "")
    conn = get_db()

    if q and smart:
        # AI semantic search (free, local sentence-transformers) - ranks by meaning not exact text
        all_alumni = conn.execute("SELECT * FROM alumni").fetchall()
        alumni = ai.semantic_search_alumni(q, all_alumni)
        account_status = {r["alumni_id"]: r["is_active"] for r in conn.execute("SELECT alumni_id, is_active FROM users WHERE alumni_id IS NOT NULL").fetchall()}
        conn.close()
        return render_template("alumni_list.html", alumni=alumni, q=q, batch=batch, dept=dept, smart=smart, account_status=account_status)

    sql = "SELECT * FROM alumni WHERE 1=1"
    params = []
    if q:
        sql += " AND (name LIKE ? OR email LIKE ? OR current_company LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if batch:
        sql += " AND batch_year = ?"
        params.append(batch)
    if dept:
        sql += " AND department LIKE ?"
        params.append(f"%{dept}%")
    sql += " ORDER BY name"
    alumni = conn.execute(sql, params).fetchall()
    account_status = {r["alumni_id"]: r["is_active"] for r in conn.execute("SELECT alumni_id, is_active FROM users WHERE alumni_id IS NOT NULL").fetchall()}
    conn.close()
    return render_template("alumni_list.html", alumni=alumni, q=q, batch=batch, dept=dept, smart=smart, account_status=account_status)


@app.route("/alumni/add", methods=["GET", "POST"])
@login_required
@admin_required
def alumni_add():
    if request.method == "POST":
        batch_year_raw = request.form.get("batch_year") or ""
        try:
            batch_year = int(batch_year_raw)
        except ValueError:
            batch_year = None
        if not batch_year or not (MIN_PASSOUT_YEAR <= batch_year <= MAX_PASSOUT_YEAR):
            flash(f"Enter a valid passout year between {MIN_PASSOUT_YEAR} and {MAX_PASSOUT_YEAR}.", "danger")
            return render_template("alumni_form.html", alumni=None, min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR, mail_on=mail_configured())
        if not is_valid_email(request.form.get("email")):
            flash("Enter a valid email address (e.g. name@example.com).", "danger")
            return render_template("alumni_form.html", alumni=None, min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR, mail_on=mail_configured())

        send_email = bool(request.form.get("send_email"))
        conn = get_db()
        try:
            conn.execute(
                """INSERT INTO alumni (name,email,phone,batch_year,department,current_company,designation,city,linkedin)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (request.form["name"], request.form["email"], request.form.get("phone"),
                 batch_year, request.form.get("department"),
                 request.form.get("current_company"), request.form.get("designation"),
                 request.form.get("city"), request.form.get("linkedin")),
            )
            alumni_id = conn.execute("SELECT id FROM alumni WHERE email = ?", (request.form["email"],)).fetchone()["id"]
            # generate a fresh random password per account instead of a shared static default
            generated_password = secrets.token_urlsafe(6)
            user_exists = conn.execute("SELECT id FROM users WHERE email = ?", (request.form["email"],)).fetchone()
            if not user_exists:
                conn.execute(
                    "INSERT INTO users (alumni_id, email, password_hash, role) VALUES (?, ?, ?, ?)",
                    (alumni_id, request.form["email"], hash_password(generated_password), "alumni"),
                )
            else:
                # a stale/orphaned user row already existed for this email (e.g. from a failed earlier
                # insert) - link it to this alumni and reset its password so the emailed credentials work
                conn.execute(
                    "UPDATE users SET alumni_id = ?, password_hash = ?, role = 'alumni', is_active = TRUE WHERE id = ?",
                    (alumni_id, hash_password(generated_password), user_exists["id"]),
                )
            conn.commit()

            emailed = False
            if send_email:
                body = (
                    f"Hi {request.form['name']},\n\n"
                    f"An alumni account has been created for you on AlumniMS.\n\n"
                    f"Login email: {request.form['email']}\n"
                    f"Password: {generated_password}\n\n"
                    f"Please log in and change your password after your first sign-in.\n\n"
                    f"Regards,\nAlumni Management Team"
                )
                emailed = send_single_email("Your AlumniMS account details", body, request.form["email"])

            if emailed:
                flash(f"Alumni added and login details emailed to {request.form['email']}.", "success")
            elif send_email:
                flash(f"Alumni added, but email could not be sent. Password: {generated_password}", "warning")
            else:
                flash(f"Alumni added. Login password: {generated_password}", "success")
            return redirect(url_for("alumni_list"))
        except IntegrityError:
            flash("Email already exists.", "danger")
        finally:
            conn.close()
    return render_template("alumni_form.html", alumni=None, min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR, mail_on=mail_configured())


@app.route("/alumni/<int:id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def alumni_edit(id):
    conn = get_db()
    if request.method == "POST":
        batch_year_raw = request.form.get("batch_year") or ""
        try:
            batch_year = int(batch_year_raw)
        except ValueError:
            batch_year = None
        if not batch_year or not (MIN_PASSOUT_YEAR <= batch_year <= MAX_PASSOUT_YEAR):
            flash(f"Enter a valid passout year between {MIN_PASSOUT_YEAR} and {MAX_PASSOUT_YEAR}.", "danger")
            alum = conn.execute("SELECT * FROM alumni WHERE id=?", (id,)).fetchone()
            conn.close()
            return render_template("alumni_form.html", alumni=alum, min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR)
        if not is_valid_email(request.form.get("email")):
            flash("Enter a valid email address (e.g. name@example.com).", "danger")
            alum = conn.execute("SELECT * FROM alumni WHERE id=?", (id,)).fetchone()
            conn.close()
            return render_template("alumni_form.html", alumni=alum, min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR)
        conn.execute(
            """UPDATE alumni SET name=?,email=?,phone=?,batch_year=?,department=?,
               current_company=?,designation=?,city=?,linkedin=? WHERE id=?""",
            (request.form["name"], request.form["email"], request.form.get("phone"),
             batch_year, request.form.get("department"),
             request.form.get("current_company"), request.form.get("designation"),
             request.form.get("city"), request.form.get("linkedin"), id),
        )
        user_row = conn.execute("SELECT id FROM users WHERE alumni_id = ?", (id,)).fetchone()
        if user_row:
            conn.execute("UPDATE users SET email = ? WHERE alumni_id = ?", (request.form["email"], id))
        conn.commit()
        conn.close()
        flash("Alumni updated.", "success")
        return redirect(url_for("alumni_list"))
    alum = conn.execute("SELECT * FROM alumni WHERE id=?", (id,)).fetchone()
    conn.close()
    return render_template("alumni_form.html", alumni=alum, min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR)


@app.route("/alumni/<int:id>/delete", methods=["POST"])
@login_required
@admin_required
def alumni_delete(id):
    conn = get_db()
    conn.execute("DELETE FROM alumni WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash("Alumni deleted.", "info")
    return redirect(url_for("alumni_list"))


@app.route("/alumni/<int:id>/suspend", methods=["POST"])
@login_required
@admin_required
def alumni_suspend(id):
    conn = get_db()
    conn.execute("UPDATE users SET is_active = FALSE WHERE alumni_id = ?", (id,))
    conn.commit()
    conn.close()
    flash("Alumni account suspended.", "info")
    return redirect(url_for("alumni_list"))


@app.route("/alumni/<int:id>/activate", methods=["POST"])
@login_required
@admin_required
def alumni_activate(id):
    conn = get_db()
    conn.execute("UPDATE users SET is_active = TRUE WHERE alumni_id = ?", (id,))
    conn.commit()
    conn.close()
    flash("Alumni account reactivated.", "success")
    return redirect(url_for("alumni_list"))


@app.route("/alumni/<int:id>/delete-account", methods=["POST"])
@login_required
@admin_required
def alumni_delete_account(id):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE alumni_id = ?", (id,))
    conn.commit()
    conn.close()
    flash("Login access removed. Alumni profile record was kept.", "info")
    return redirect(url_for("alumni_list"))


@app.route("/profile/edit", methods=["GET", "POST"])
@login_required
def profile_edit():
    alumni_id = session.get("alumni_id")
    if not alumni_id:
        flash("No alumni profile linked to this account.", "warning")
        return redirect(url_for("dashboard"))

    conn = get_db()
    if request.method == "POST":
        batch_year_raw = request.form.get("batch_year") or ""
        try:
            batch_year = int(batch_year_raw)
        except ValueError:
            batch_year = None
        if not batch_year or not (MIN_PASSOUT_YEAR <= batch_year <= MAX_PASSOUT_YEAR):
            flash(f"Enter a valid passout year between {MIN_PASSOUT_YEAR} and {MAX_PASSOUT_YEAR}.", "danger")
            alum = conn.execute("SELECT * FROM alumni WHERE id=?", (alumni_id,)).fetchone()
            conn.close()
            return render_template("profile_edit.html", alumni=alum, min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR)

        current = conn.execute("SELECT photo_path FROM alumni WHERE id=?", (alumni_id,)).fetchone()
        old_photo_path = current["photo_path"] if current else None
        photo_path = old_photo_path
        if request.form.get("remove_photo"):
            if old_photo_path:
                old_full_path = os.path.join(app.root_path, "static", old_photo_path.lstrip("/"))
                if os.path.isfile(old_full_path):
                    try:
                        os.remove(old_full_path)
                    except OSError:
                        pass
            photo_path = None
        else:
            new_photo_path = save_profile_photo(request.files.get("photo"), alumni_id, old_photo_path)
            if new_photo_path:
                photo_path = new_photo_path

        conn.execute(
            """UPDATE alumni SET name=?,email=?,phone=?,batch_year=?,department=?,
               current_company=?,designation=?,city=?,linkedin=?,networking_opt_in=?,photo_path=? WHERE id=?""",
            (request.form["name"], request.form["email"], request.form.get("phone"),
             batch_year, request.form.get("department"),
             request.form.get("current_company"), request.form.get("designation"),
             request.form.get("city"), request.form.get("linkedin"),
             bool(request.form.get("networking_opt_in")), photo_path, alumni_id),
        )
        conn.execute("UPDATE users SET email = ? WHERE alumni_id = ?", (request.form["email"], alumni_id))
        conn.commit()
        conn.close()
        session["email"] = request.form["email"]
        flash("Profile updated.", "success")
        return redirect(url_for("dashboard"))
    alum = conn.execute("SELECT * FROM alumni WHERE id=?", (alumni_id,)).fetchone()
    conn.close()
    return render_template("profile_edit.html", alumni=alum, min_year=MIN_PASSOUT_YEAR, max_year=MAX_PASSOUT_YEAR)


@app.route("/alumni/<int:id>")
@login_required
def alumni_profile(id):
    conn = get_db()
    alum = conn.execute("SELECT * FROM alumni WHERE id=?", (id,)).fetchone()
    donations = conn.execute("SELECT * FROM donations WHERE alumni_id=? ORDER BY donation_date DESC", (id,)).fetchall()
    events = conn.execute("""SELECT e.* FROM events e JOIN event_registrations r ON e.id=r.event_id
                              WHERE r.alumni_id=? ORDER BY e.event_date""", (id,)).fetchall()
    conn.close()
    return render_template("alumni_profile.html", alum=alum, donations=donations, events=events)


# ---------- EVENTS ----------
@app.route("/events")
@login_required
def events_list():
    conn = get_db()
    events = conn.execute("""SELECT e.*, (SELECT COUNT(*) FROM event_registrations r WHERE r.event_id=e.id) reg_count
                              FROM events e ORDER BY event_date DESC""").fetchall()
    conn.close()
    return render_template("events_list.html", events=events)


@app.route("/events/add", methods=["GET", "POST"])
@login_required
@admin_required
def events_add():
    if request.method == "POST":
        conn = get_db()
        conn.execute("INSERT INTO events (title,description,event_date,location) VALUES (?,?,?,?)",
                     (request.form["title"], request.form.get("description"),
                      request.form.get("event_date"), request.form.get("location")))
        conn.commit()
        if request.form.get("notify"):
            emails = [r["email"] for r in conn.execute("SELECT email FROM alumni").fetchall()]
            admin_emails = [r["email"] for r in conn.execute("SELECT email FROM users WHERE role='admin'").fetchall()]
            emails = list(dict.fromkeys(emails + admin_emails))
            body = f"New event: {request.form['title']}\n{request.form.get('description','')}\nDate: {request.form.get('event_date','TBA')}\nLocation: {request.form.get('location','TBA')}"
            result = send_bulk_email(f"New Event: {request.form['title']}", body, emails)
            if result["skipped"]:
                flash("Event created. Email not sent (configure MAIL_USERNAME/MAIL_PASSWORD in .env).", "warning")
            else:
                flash(f"Event created. Notified {result['sent']} alumni by email.", "success")
        else:
            flash("Event created.", "success")
        conn.close()
        return redirect(url_for("events_list"))
    return render_template("event_form.html", mail_on=mail_configured())


@app.route("/events/<int:id>/delete", methods=["POST"])
@login_required
@admin_required
def events_delete(id):
    conn = get_db()
    conn.execute("DELETE FROM events WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash("Event deleted.", "info")
    return redirect(url_for("events_list"))


@app.route("/events/<int:id>")
@login_required
def events_detail(id):
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id=?", (id,)).fetchone()
    registered = conn.execute("""SELECT a.* FROM alumni a JOIN event_registrations r ON a.id=r.alumni_id
                                  WHERE r.event_id=?""", (id,)).fetchall()
    is_admin = session.get("role") == "admin"
    all_alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall() if is_admin else []
    already_registered = False
    if not is_admin and session.get("alumni_id"):
        already_registered = any(a["id"] == session["alumni_id"] for a in registered)
    conn.close()
    return render_template("event_detail.html", event=event, registered=registered, all_alumni=all_alumni,
                            is_admin=is_admin, already_registered=already_registered)


@app.route("/events/<int:id>/register", methods=["POST"])
@login_required
def events_register(id):
    is_admin = session.get("role") == "admin"
    if is_admin:
        # admins may register any alumni on their behalf
        alumni_id = request.form.get("alumni_id")
    else:
        # alumni can only register themselves, regardless of what the form submits
        alumni_id = session.get("alumni_id")
    if not alumni_id:
        flash("No alumni account linked to this login.", "danger")
        return redirect(url_for("events_detail", id=id))
    conn = get_db()
    try:
        conn.execute("INSERT INTO event_registrations (event_id,alumni_id) VALUES (?,?)", (id, alumni_id))
        conn.commit()
        flash("Registered for event.", "success")
    except IntegrityError:
        flash("Already registered.", "warning")
    conn.close()
    return redirect(url_for("events_detail", id=id))


# ---------- DONATIONS ----------
def send_donation_thankyou(donation_id):
    """Runs in the background scheduler, 2 minutes after a donation is recorded."""
    conn = get_db()
    donation = conn.execute(
        """SELECT d.*, a.name alumni_name, a.email alumni_email FROM donations d
           JOIN alumni a ON a.id=d.alumni_id WHERE d.id=?""",
        (donation_id,),
    ).fetchone()
    conn.close()
    if not donation or not donation.get("alumni_email"):
        return
    result = ai.generate_donation_thankyou(donation["alumni_name"], donation["amount"], donation.get("purpose") or "")
    body = result.get("text") or (
        f"Dear {donation['alumni_name']},\n\nThank you so much for your generous donation of "
        f"\u20b9{donation['amount']}. Your support means a lot to our alumni community.\n\n"
        f"Regards,\nAlumni Management Team"
    )
    send_single_email("Thank you for your donation - AlumniMS", body, donation["alumni_email"])

    conn = get_db()
    admin_emails = [r["email"] for r in conn.execute("SELECT email FROM users WHERE role='admin'").fetchall()]
    conn.close()
    admin_body = (f"{donation['alumni_name']} donated \u20b9{donation['amount']} "
                  f"({donation.get('purpose') or 'general'}).")
    send_bulk_email("New donation received - AlumniMS", admin_body, admin_emails)


def schedule_donation_thankyou(donation_id):
    run_date = datetime.now() + timedelta(minutes=2)
    scheduler.add_job(send_donation_thankyou, "date", run_date=run_date, args=[donation_id],
                       id=f"donation_thankyou_{donation_id}", replace_existing=True)


@app.route("/donations")
@login_required
@admin_required
def donations_list():
    conn = get_db()
    donations = conn.execute("""SELECT d.*, a.name alumni_name FROM donations d
                                 JOIN alumni a ON a.id=d.alumni_id ORDER BY donation_date DESC""").fetchall()
    total = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM donations").fetchone()["s"]
    all_alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
    conn.close()
    return render_template("donations_list.html", donations=donations, total=total, all_alumni=all_alumni)


@app.route("/donations/add", methods=["POST"])
@login_required
@admin_required
def donations_add():
    conn = get_db()
    row = conn.execute(
        "INSERT INTO donations (alumni_id,amount,purpose,donation_date,payment_status) VALUES (?,?,?,?,?) RETURNING id",
        (request.form["alumni_id"], request.form["amount"], request.form.get("purpose"),
         request.form.get("donation_date") or datetime.now().strftime("%Y-%m-%d"), "manual"),
    ).fetchone()
    conn.commit()
    conn.close()
    schedule_donation_thankyou(row["id"])
    flash("Donation recorded.", "success")
    return redirect(url_for("donations_list"))


@app.route("/donations/<int:id>/delete", methods=["POST"])
@login_required
@admin_required
def donations_delete(id):
    conn = get_db()
    conn.execute("DELETE FROM donations WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("donations_list"))


# ---------- ONLINE DONATIONS (Razorpay) ----------
@app.route("/donate", methods=["GET"])
@login_required
def donate_page():
    if not session.get("alumni_id"):
        flash("Only alumni accounts can donate online.", "warning")
        return redirect(url_for("dashboard"))
    return render_template("donate.html", razorpay_enabled=bool(razorpay_client), razorpay_key_id=RAZORPAY_KEY_ID)


@app.route("/donate/create-order", methods=["POST"])
@login_required
def donate_create_order():
    if not session.get("alumni_id"):
        return jsonify({"error": "Only alumni accounts can donate."}), 403
    if not razorpay_client:
        return jsonify({"error": "Payment gateway is not configured. Set RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET."}), 503

    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid amount."}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be greater than zero."}), 400
    purpose = (data.get("purpose") or "").strip()[:200]

    order = razorpay_client.order.create({
        "amount": int(round(amount * 100)),  # paise
        "currency": "INR",
        "payment_capture": 1,
    })

    conn = get_db()
    row = conn.execute(
        "INSERT INTO donations (alumni_id,amount,purpose,donation_date,payment_status,razorpay_order_id) "
        "VALUES (?,?,?,?,?,?) RETURNING id",
        (session["alumni_id"], amount, purpose, datetime.now().strftime("%Y-%m-%d"), "pending", order["id"]),
    ).fetchone()
    conn.commit()
    conn.close()

    return jsonify({
        "order_id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "key_id": RAZORPAY_KEY_ID,
        "donation_id": row["id"],
    })


@app.route("/donate/verify", methods=["POST"])
@login_required
def donate_verify():
    if not session.get("alumni_id"):
        return jsonify({"error": "Only alumni accounts can donate."}), 403
    if not razorpay_client:
        return jsonify({"error": "Payment gateway is not configured."}), 503

    data = request.get_json(silent=True) or {}
    order_id = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature = data.get("razorpay_signature")
    if not (order_id and payment_id and signature):
        return jsonify({"error": "Missing payment details."}), 400

    try:
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"error": "Payment verification failed."}), 400

    conn = get_db()
    donation = conn.execute(
        "SELECT * FROM donations WHERE razorpay_order_id=? AND alumni_id=?",
        (order_id, session["alumni_id"]),
    ).fetchone()
    if not donation:
        conn.close()
        return jsonify({"error": "Donation record not found."}), 404

    conn.execute(
        "UPDATE donations SET payment_status='paid', razorpay_payment_id=? WHERE id=?",
        (payment_id, donation["id"]),
    )
    conn.commit()
    conn.close()
    schedule_donation_thankyou(donation["id"])
    flash("Thank you! Your donation was received successfully.", "success")
    return jsonify({"ok": True})


# ---------- JOBS ----------
@app.route("/jobs")
@login_required
def jobs_list():
    conn = get_db()
    jobs = conn.execute("""SELECT j.*, a.name posted_by_name FROM jobs j
                            LEFT JOIN alumni a ON a.id=j.posted_by_id ORDER BY posted_at DESC""").fetchall()
    all_alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()

    my_interests = {}
    interested_by_job = {}
    if session.get("role") == "admin":
        rows = conn.execute("""SELECT ji.job_id, a.name, a.email FROM job_interests ji
                                JOIN alumni a ON a.id=ji.alumni_id
                                WHERE ji.interested = TRUE ORDER BY ji.created_at DESC""").fetchall()
        for r in rows:
            interested_by_job.setdefault(r["job_id"], []).append({"name": r["name"], "email": r["email"]})
    elif session.get("alumni_id"):
        rows = conn.execute("SELECT job_id, interested FROM job_interests WHERE alumni_id = ?",
                             (session.get("alumni_id"),)).fetchall()
        my_interests = {r["job_id"]: r["interested"] for r in rows}

    conn.close()
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        all_alumni=all_alumni,
        mail_on=mail_configured(),
        my_interests=my_interests,
        interested_by_job=interested_by_job,
    )


@app.route("/jobs/add", methods=["POST"])
@login_required
@admin_required
def jobs_add():
    conn = get_db()
    conn.execute("INSERT INTO jobs (posted_by_id,title,company,location,salary,description) VALUES (?,?,?,?,?,?)",
                 (request.form.get("posted_by_id") or None, request.form["title"],
                  request.form.get("company"), request.form.get("location"),
                  request.form.get("salary"), request.form.get("description")))
    conn.commit()

    # every new job is announced to all alumni and admins by email (if mail is configured)
    emails = [r["email"] for r in conn.execute("SELECT email FROM alumni").fetchall()]
    admin_emails = [r["email"] for r in conn.execute("SELECT email FROM users WHERE role='admin'").fetchall()]
    emails = list(dict.fromkeys(emails + admin_emails))
    salary_line = f"\nSalary: {request.form['salary']}" if request.form.get("salary") else ""
    body = (f"New job posted: {request.form['title']} at {request.form.get('company','-')}\n"
            f"Location: {request.form.get('location','-')}{salary_line}\n{request.form.get('description','')}")
    result = send_bulk_email(f"New Job: {request.form['title']}", body, emails)
    if result["skipped"]:
        flash("Job posted. Email not sent (configure MAIL_USERNAME/MAIL_PASSWORD in .env).", "warning")
    else:
        flash(f"Job posted. Notified {result['sent']} alumni by email.", "success")
    conn.close()
    return redirect(url_for("jobs_list"))


@app.route("/jobs/<int:id>/delete", methods=["POST"])
@login_required
@admin_required
def jobs_delete(id):
    conn = get_db()
    conn.execute("DELETE FROM jobs WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("jobs_list"))


@app.route("/jobs/<int:id>/interest", methods=["POST"])
@login_required
def jobs_interest(id):
    alumni_id = session.get("alumni_id")
    if not alumni_id:
        flash("Only alumni accounts can respond to job posts.", "warning")
        return redirect(url_for("jobs_list"))

    interested = request.form.get("interested") == "yes"
    conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
    alum = conn.execute("SELECT * FROM alumni WHERE id=?", (alumni_id,)).fetchone()
    existing = conn.execute("SELECT id FROM job_interests WHERE job_id=? AND alumni_id=?", (id, alumni_id)).fetchone()
    if existing:
        conn.execute("UPDATE job_interests SET interested=?, created_at=CURRENT_TIMESTAMP WHERE id=?",
                     (interested, existing["id"]))
    else:
        conn.execute("INSERT INTO job_interests (job_id, alumni_id, interested) VALUES (?,?,?)",
                     (id, alumni_id, interested))
    conn.commit()

    if interested and job and alum:
        admin_emails = [r["email"] for r in conn.execute("SELECT email FROM users WHERE role='admin'").fetchall()]
        body = f"{alum['name']} ({alum['email']}) is interested in the job: {job['title']} at {job.get('company') or '-'}."
        send_bulk_email(f"Job interest: {job['title']}", body, admin_emails)

    conn.close()
    flash("Thanks! Your response has been recorded." if interested else "Marked as not interested.", "success")
    return redirect(url_for("jobs_list"))


# ---------- AI FEATURES ----------
@app.route("/ai/chat")
@login_required
def ai_chat_page():
    return render_template("ai_chat.html", gemini_on=ai.gemini_available())


@app.route("/ai/chat/ask", methods=["POST"])
@login_required
def ai_chat_ask():
    data = request.json or {}
    question = data.get("question", "")
    history = data.get("history", [])
    current_user = {"email": session.get("email"), "alumni_id": session.get("alumni_id"), "role": session.get("role")}

    # mentor matching isn't stored in the DB (it's computed live), so the NL->SQL path can never
    # answer it - handle "mentor" questions directly using the same logic as the Career Mentor page
    if "mentor" in question.lower() and session.get("role") != "admin" and session.get("alumni_id"):
        conn = get_db()
        alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
        current = conn.execute("SELECT * FROM alumni WHERE id = ?", (session["alumni_id"],)).fetchone()
        conn.close()
        top = ai.recommend_best_mentor(current, alumni, "career guidance", top_k=1) if current else []
        if top:
            m = top[0]
            return jsonify({"answer": f"Your top career mentor match is {m['name']} ({m['email']}) - {m['reason']}."})
        return jsonify({"answer": "No suitable mentor match was found for your profile yet."})

    # admins asking about mentor matches for a specific named alumnus
    if "mentor" in question.lower() and session.get("role") == "admin":
        conn = get_db()
        alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
        conn.close()
        named = next(
            (a for a in alumni if a["name"] and a["name"].lower() in question.lower()), None
        )
        if named:
            top = ai.recommend_best_mentor(named, alumni, "career guidance", top_k=1)
            if top:
                m = top[0]
                return jsonify({"answer": f"{named['name']}'s top career mentor match is {m['name']} ({m['email']}) - {m['reason']}."})
            return jsonify({"answer": f"No suitable mentor match was found for {named['name']}."})
        return jsonify({"answer": "Couldn't find an alumnus with that name. See the Mentor Matches page for the full overview."})

    result = ai.ask_alumni_chatbot(question, history, current_user=current_user)
    return jsonify(result)


@app.route("/ai/generate/event", methods=["POST"])
@login_required
def ai_generate_event():
    data = request.json
    result = ai.generate_event_description(
        data.get("title", ""), data.get("bullets", ""), data.get("location", ""), data.get("date", "")
    )
    return jsonify(result)


@app.route("/ai/generate/thankyou", methods=["POST"])
@login_required
def ai_generate_thankyou():
    data = request.json
    result = ai.generate_donation_thankyou(data.get("name", ""), data.get("amount", ""), data.get("purpose", ""))
    return jsonify(result)


# ---------- REPORTS & VISUALS ----------
@app.route("/reports")
@login_required
@admin_required
def reports_page():
    conn = get_db()
    by_batch = conn.execute("SELECT batch_year, COUNT(*) c FROM alumni WHERE batch_year IS NOT NULL GROUP BY batch_year ORDER BY batch_year").fetchall()
    by_dept = conn.execute("SELECT department, COUNT(*) c FROM alumni WHERE department IS NOT NULL AND department!='' GROUP BY department").fetchall()
    by_year_donation = conn.execute("""SELECT substr(donation_date,1,4) yr, SUM(amount) s
                                        FROM donations GROUP BY yr ORDER BY yr""").fetchall()
    top_donors = conn.execute("""SELECT a.name, SUM(d.amount) total FROM donations d JOIN alumni a ON a.id=d.alumni_id
                                  GROUP BY d.alumni_id, a.name ORDER BY total DESC LIMIT 5""").fetchall()
    conn.close()
    return render_template("reports.html",
        batch_labels=[r["batch_year"] for r in by_batch], batch_data=[r["c"] for r in by_batch],
        dept_labels=[r["department"] for r in by_dept], dept_data=[r["c"] for r in by_dept],
        year_labels=[r["yr"] for r in by_year_donation], year_data=[r["s"] for r in by_year_donation],
        top_donors=top_donors)


@app.route("/reports/alumni/excel")
@login_required
@admin_required
def export_alumni_excel():
    conn = get_db()
    rows = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
    conn.close()
    buf = reports.alumni_excel(rows)
    return send_file(buf, download_name="alumni_directory.xlsx", as_attachment=True)


@app.route("/reports/alumni/pdf")
@login_required
@admin_required
def export_alumni_pdf():
    conn = get_db()
    rows = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
    conn.close()
    buf = reports.alumni_pdf(rows)
    return send_file(buf, download_name="alumni_directory.pdf", as_attachment=True)


@app.route("/reports/donations/excel")
@login_required
@admin_required
def export_donations_excel():
    conn = get_db()
    rows = conn.execute("""SELECT d.*, a.name alumni_name FROM donations d
                            JOIN alumni a ON a.id=d.alumni_id ORDER BY donation_date DESC""").fetchall()
    total = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM donations").fetchone()["s"]
    conn.close()
    buf = reports.donations_excel(rows, total)
    return send_file(buf, download_name="donations_report.xlsx", as_attachment=True)


@app.route("/reports/donations/pdf")
@login_required
@admin_required
def export_donations_pdf():
    conn = get_db()
    rows = conn.execute("""SELECT d.*, a.name alumni_name FROM donations d
                            JOIN alumni a ON a.id=d.alumni_id ORDER BY donation_date DESC""").fetchall()
    total = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM donations").fetchone()["s"]
    conn.close()
    buf = reports.donations_pdf(rows, total)
    return send_file(buf, download_name="donations_report.pdf", as_attachment=True)


@app.route("/mentor/recommend")
@login_required
def mentor_recommend():
    if session.get("role") == "admin":
        # admins get an overview of best mentor matches instead of a personal recommendation
        return redirect(url_for("mentor_overview"))
    goal = request.args.get("goal", "career guidance")
    conn = get_db()
    alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
    # always use the logged-in alumnus's own id - never trust a client-supplied alumni_id here,
    # otherwise anyone could view another alumnus's recommendation by editing the query string
    alumnus_id = session.get("alumni_id")
    current = conn.execute("SELECT * FROM alumni WHERE id = ?", (alumnus_id,)).fetchone() if alumnus_id else None
    recommendations = ai.recommend_best_mentor(current, alumni, goal, top_k=1) if current else []
    conn.close()
    return render_template("mentor_recommend.html", recommendations=recommendations, goal=goal, current=current, alumni=alumni)


@app.route("/mentor/overview")
@login_required
@admin_required
def mentor_overview():
    """For each alumnus, shows the top-recommended mentor - admin visibility only, no self-service."""
    conn = get_db()
    alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
    pairings = []
    for alum in alumni:
        top = ai.recommend_best_mentor(alum, alumni, "career guidance", top_k=1)
        pairings.append({"alumnus": alum, "mentor": top[0] if top else None})
    conn.close()
    return render_template("mentor_overview.html", pairings=pairings)


# ---------- MESSAGES ----------
@app.route("/messages")
@login_required
def messages_inbox():
    alumni_id = session.get("alumni_id")
    if not alumni_id:
        flash("Only alumni accounts can use direct messaging.", "warning")
        return redirect(url_for("dashboard"))
    conn = get_db()
    conversations = conn.execute("""
        SELECT a.id AS alumni_id, a.name, a.email,
               (SELECT m2.body FROM messages m2
                WHERE (m2.sender_id = a.id AND m2.recipient_id = ?) OR (m2.sender_id = ? AND m2.recipient_id = a.id)
                ORDER BY m2.created_at DESC LIMIT 1) AS last_body,
               (SELECT m2.created_at FROM messages m2
                WHERE (m2.sender_id = a.id AND m2.recipient_id = ?) OR (m2.sender_id = ? AND m2.recipient_id = a.id)
                ORDER BY m2.created_at DESC LIMIT 1) AS last_at,
               (SELECT COUNT(*) FROM messages m3 WHERE m3.sender_id = a.id AND m3.recipient_id = ? AND m3.is_read = FALSE) AS unread_count
        FROM alumni a
        WHERE a.id != ? AND EXISTS (
            SELECT 1 FROM messages m
            WHERE (m.sender_id = a.id AND m.recipient_id = ?) OR (m.sender_id = ? AND m.recipient_id = a.id)
        )
        ORDER BY last_at DESC
    """, (alumni_id, alumni_id, alumni_id, alumni_id, alumni_id, alumni_id, alumni_id, alumni_id)).fetchall()
    conn.close()
    return render_template("messages_inbox.html", conversations=conversations)


@app.route("/messages/<int:alumni_id>", methods=["GET", "POST"])
@login_required
def messages_thread(alumni_id):
    my_id = session.get("alumni_id")
    if not my_id:
        flash("Only alumni accounts can use direct messaging.", "warning")
        return redirect(url_for("dashboard"))
    if alumni_id == my_id:
        flash("You cannot message yourself.", "warning")
        return redirect(url_for("messages_inbox"))

    conn = get_db()
    other = conn.execute("SELECT * FROM alumni WHERE id = ?", (alumni_id,)).fetchone()
    if not other:
        conn.close()
        flash("Alumnus not found.", "danger")
        return redirect(url_for("messages_inbox"))

    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if body:
            conn.execute("INSERT INTO messages (sender_id, recipient_id, body) VALUES (?,?,?)",
                         (my_id, alumni_id, body))
            conn.commit()
        conn.close()
        return redirect(url_for("messages_thread", alumni_id=alumni_id))

    # mark incoming messages from this conversation as read
    conn.execute("UPDATE messages SET is_read = TRUE WHERE sender_id = ? AND recipient_id = ? AND is_read = FALSE",
                 (alumni_id, my_id))
    conn.commit()
    thread = conn.execute("""
        SELECT * FROM messages
        WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
        ORDER BY created_at ASC
    """, (my_id, alumni_id, alumni_id, my_id)).fetchall()
    conn.close()
    return render_template("messages_thread.html", other=other, thread=thread, my_id=my_id)


# ---------- SERENDIPITY ENGINE (AI random-coffee matching) ----------
def run_serendipity_matching():
    """Pairs up opted-in alumni who haven't been matched before, seeds an AI icebreaker
    message in their inbox, and emails both. Returns the number of new pairs made."""
    conn = get_db()
    candidates = conn.execute("""
        SELECT a.* FROM alumni a JOIN users u ON u.alumni_id = a.id
        WHERE a.networking_opt_in = TRUE AND u.is_active = TRUE
    """).fetchall()
    existing = conn.execute("SELECT alumni_a_id, alumni_b_id FROM connections").fetchall()
    matched_pairs = {(r["alumni_a_id"], r["alumni_b_id"]) for r in existing}

    pool = [dict(a) for a in candidates]
    random.shuffle(pool)
    used_ids = set()
    new_pairs = []
    for i, a in enumerate(pool):
        if a["id"] in used_ids:
            continue
        for b in pool[i + 1:]:
            if b["id"] in used_ids:
                continue
            key = tuple(sorted([a["id"], b["id"]]))
            if key in matched_pairs:
                continue
            used_ids.add(a["id"])
            used_ids.add(b["id"])
            new_pairs.append((a, b))
            break

    for a, b in new_pairs:
        a_id, b_id = sorted([a["id"], b["id"]])
        conn.execute("INSERT INTO connections (alumni_a_id, alumni_b_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                     (a_id, b_id))
        icebreaker = ai.generate_serendipity_icebreaker(a, b)
        conn.execute("INSERT INTO messages (sender_id, recipient_id, body) VALUES (?,?,?)",
                     (a["id"], b["id"], icebreaker))
        conn.commit()
        email_body = f"{icebreaker}\n\nLog in to AlumniMS and check your Messages inbox to reply."
        send_single_email("You've been matched this week! - AlumniMS", email_body, a["email"])
        send_single_email("You've been matched this week! - AlumniMS", email_body, b["email"])

    conn.close()
    return len(new_pairs)


@app.route("/matches")
@login_required
@admin_required
def matches_overview():
    conn = get_db()
    history = conn.execute("""
        SELECT c.*, x.name AS a_name, y.name AS b_name
        FROM connections c
        JOIN alumni x ON x.id = c.alumni_a_id
        JOIN alumni y ON y.id = c.alumni_b_id
        ORDER BY c.matched_at DESC
    """).fetchall()
    opted_in_count = conn.execute("""
        SELECT COUNT(*) c FROM alumni a JOIN users u ON u.alumni_id = a.id
        WHERE a.networking_opt_in = TRUE AND u.is_active = TRUE
    """).fetchone()["c"]
    conn.close()
    return render_template("matches_overview.html", history=history, opted_in_count=opted_in_count)


@app.route("/matches/run", methods=["POST"])
@login_required
@admin_required
def matches_run():
    count = run_serendipity_matching()
    flash(f"Matching complete - {count} new pair(s) introduced.", "success")
    return redirect(url_for("matches_overview"))


init_db()  # runs on both `python app.py` and gunicorn/production startup

# weekly serendipity matching - guarded so Flask's debug reloader doesn't start it twice
if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    scheduler.add_job(run_serendipity_matching, "cron", day_of_week="mon", hour=9, id="serendipity_matching")

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug_mode, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
