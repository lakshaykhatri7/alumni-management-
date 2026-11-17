"""
PostgreSQL connection helper. Mimics the small subset of the sqlite3 API this app
uses (conn.execute(sql, params).fetchone()/.fetchall(), dict-like rows, commit/close)
so the rest of app.py barely changes. Connect pgAdmin4 to the same DB (see .env) to
browse/edit data visually.
"""
import os
import psycopg2
import psycopg2.extras
from psycopg2 import errors as pg_errors

IntegrityError = pg_errors.UniqueViolation


class Cursor:
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class Connection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        # translate sqlite-style "?" placeholders to psycopg2-style "%s"
        sql = sql.replace("?", "%s")
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(sql, params or [])
        except Exception:
            self._conn.rollback()
            raise
        return Cursor(cur)

    def executescript(self, sql):
        cur = self._conn.cursor()
        cur.execute(sql)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def cursor(self):
        return self._conn.cursor()


def get_db():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        # Render/Railway/Heroku-style single connection string (used in production)
        conn = psycopg2.connect(database_url, sslmode="require")
    else:
        # local dev - separate host/port/name/user/password vars
        conn = psycopg2.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=os.environ.get("DB_PORT", "5432"),
            dbname=os.environ.get("DB_NAME", "alumni_db"),
            user=os.environ.get("DB_USER", "postgres"),
            password=os.environ.get("DB_PASSWORD", ""),
        )
    return Connection(conn)


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS alumni (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        phone TEXT,
        batch_year INTEGER,
        department TEXT,
        current_company TEXT,
        designation TEXT,
        city TEXT,
        linkedin TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS events (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        event_date TEXT,
        location TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS event_registrations (
        id SERIAL PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        alumni_id INTEGER NOT NULL REFERENCES alumni(id) ON DELETE CASCADE,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(event_id, alumni_id)
    );

    CREATE TABLE IF NOT EXISTS donations (
        id SERIAL PRIMARY KEY,
        alumni_id INTEGER NOT NULL REFERENCES alumni(id) ON DELETE CASCADE,
        amount REAL NOT NULL,
        purpose TEXT,
        donation_date TEXT DEFAULT CURRENT_TIMESTAMP,
        payment_status TEXT NOT NULL DEFAULT 'manual',
        razorpay_order_id TEXT,
        razorpay_payment_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS jobs (
        id SERIAL PRIMARY KEY,
        posted_by_id INTEGER REFERENCES alumni(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        company TEXT,
        location TEXT,
        salary TEXT,
        description TEXT,
        posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        alumni_id INTEGER UNIQUE REFERENCES alumni(id) ON DELETE SET NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'alumni',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS job_interests (
        id SERIAL PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        alumni_id INTEGER NOT NULL REFERENCES alumni(id) ON DELETE CASCADE,
        interested BOOLEAN NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(job_id, alumni_id)
    );

    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        sender_id INTEGER NOT NULL REFERENCES alumni(id) ON DELETE CASCADE,
        recipient_id INTEGER NOT NULL REFERENCES alumni(id) ON DELETE CASCADE,
        body TEXT NOT NULL,
        is_read BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS connections (
        id SERIAL PRIMARY KEY,
        alumni_a_id INTEGER NOT NULL REFERENCES alumni(id) ON DELETE CASCADE,
        alumni_b_id INTEGER NOT NULL REFERENCES alumni(id) ON DELETE CASCADE,
        matched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(alumni_a_id, alumni_b_id)
    );
    """)
    # backfill is_active for databases created before this column existed
    conn.executescript("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;")
    conn.executescript("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS salary TEXT;")
    conn.executescript("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_hash TEXT;")
    conn.executescript("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expires TIMESTAMP;")
    conn.executescript("ALTER TABLE donations ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'manual';")
    conn.executescript("ALTER TABLE donations ADD COLUMN IF NOT EXISTS razorpay_order_id TEXT;")
    conn.executescript("ALTER TABLE donations ADD COLUMN IF NOT EXISTS razorpay_payment_id TEXT;")
    conn.executescript("ALTER TABLE alumni ADD COLUMN IF NOT EXISTS networking_opt_in BOOLEAN NOT NULL DEFAULT TRUE;")
    conn.executescript("ALTER TABLE alumni ADD COLUMN IF NOT EXISTS photo_path TEXT;")

    admin_email = os.environ.get("ADMIN_EMAIL", "admin@alumni.local")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    default_alumni_password = os.environ.get("ALUMNI_DEFAULT_PASSWORD", "alumni123")

    admin_password_hash = __import__("hashlib").sha256(admin_password.encode()).hexdigest()
    admin_row = conn.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
    if not admin_row:
        conn.execute(
            "INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
            (admin_email, admin_password_hash, "admin"),
        )
    else:
        # keep the admin account in sync with ADMIN_EMAIL/ADMIN_PASSWORD env vars on every startup,
        # so a stale password_hash from an earlier run/env config never locks the admin out
        conn.execute(
            "UPDATE users SET password_hash = ?, role = 'admin', is_active = TRUE WHERE id = ?",
            (admin_password_hash, admin_row["id"]),
        )

    # NOTE: alumni created outside signup (e.g. legacy imports) still get a default-password login
    alumni_rows = conn.execute("SELECT * FROM alumni ORDER BY id").fetchall()
    for alum in alumni_rows:
        user_row = conn.execute("SELECT id FROM users WHERE alumni_id = ?", (alum["id"],)).fetchone()
        if user_row:
            continue
        email_row = conn.execute("SELECT id FROM users WHERE email = ?", (alum["email"],)).fetchone()
        if email_row:
            # email already used by a different user account (data inconsistency) - skip rather than crash
            continue
        conn.execute(
            "INSERT INTO users (alumni_id, email, password_hash, role) VALUES (?, ?, ?, ?)",
            (alum["id"], alum["email"], __import__("hashlib").sha256(default_alumni_password.encode()).hexdigest(), "alumni"),
        )

    conn.commit()
    conn.close()
