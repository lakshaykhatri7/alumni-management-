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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS jobs (
        id SERIAL PRIMARY KEY,
        posted_by_id INTEGER REFERENCES alumni(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        company TEXT,
        location TEXT,
        description TEXT,
        posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()
