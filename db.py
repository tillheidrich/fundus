"""SQLite storage: users (username + password, no email), per-user Instagram
cookies, and moderation (block by username or IP). No content history — the
tool is deliberately stateless about what users download."""
import os
import time
import secrets
import hashlib
import sqlite3
from pathlib import Path

# Relative by default, which is right for the container: its working directory
# is the application directory and the volume is mounted there. Overridable
# because the desktop app is the opposite case — its working directory is
# inside the signed application bundle, which must not be written to and is
# replaced wholesale by the next update. Accounts belonging to the bundle
# rather than the user is how an update silently takes them away.
DATA_DIR = Path(os.environ.get("DATA_DIR") or "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "app.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")     # tolerate concurrent readers/writers
    # Deleted rows are overwritten, not just unlinked: "delete history" should
    # mean the text is gone from the file, not merely from the index.
    c.execute("PRAGMA secure_delete=ON")
    return c


def init_db():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                pw_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                api_token TEXT UNIQUE NOT NULL,
                is_admin INTEGER DEFAULT 0,
                blocked INTEGER DEFAULT 0,
                ig_cookies TEXT DEFAULT '',
                last_ip TEXT DEFAULT '',
                created_at REAL
            );
            DROP TABLE IF EXISTS library;
            CREATE TABLE IF NOT EXISTS blocked_ips (
                ip TEXT PRIMARY KEY,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            -- Desktop only: what the person on this Mac fetched, so they can
            -- find it again. A server never writes here (see main._history_on).
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL,
                kind TEXT,
                url TEXT,
                title TEXT,
                show TEXT DEFAULT '',
                feed TEXT DEFAULT '',
                guid TEXT DEFAULT '',
                published TEXT DEFAULT ''
            );
            """
        )
        # Separate cookie stores per platform: YouTube cookies expire far
        # sooner than Instagram's, and you want to replace one without
        # losing the other. Added later, hence the guarded migration.
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "yt_cookies" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN yt_cookies TEXT DEFAULT ''")


# ── History (desktop only) ─────────────────────────────────────────────────────

HISTORY_MAX = 2000      # oldest entries fall off; a history is not an archive


def add_history(kind: str, url: str, title: str = "", show: str = "", feed: str = "",
                guid: str = "", published: str = ""):
    with _conn() as c:
        c.execute("INSERT INTO history (created_at, kind, url, title, show, feed, guid, published)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (time.time(), kind, url[:2000], (title or "")[:500], (show or "")[:300],
                   (feed or "")[:2000], (guid or "")[:500], (published or "")[:100]))
        c.execute("DELETE FROM history WHERE id NOT IN"
                  " (SELECT id FROM history ORDER BY id DESC LIMIT ?)", (HISTORY_MAX,))


def list_history(limit: int = 300) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def delete_history(hid: int) -> int:
    with _conn() as c:
        return c.execute("DELETE FROM history WHERE id=?", (hid,)).rowcount


def clear_history() -> int:
    with _conn() as c:
        return c.execute("DELETE FROM history").rowcount


def history_feeds() -> list[dict]:
    """Every podcast feed seen, with the newest episode date and the GUIDs
    already fetched — enough to tell which episodes are new."""
    with _conn() as c:
        rows = c.execute("SELECT feed, show, guid, published FROM history"
                         " WHERE feed != '' ORDER BY id DESC").fetchall()
    feeds: dict = {}
    for r in rows:
        f = feeds.setdefault(r["feed"], {"feed": r["feed"], "show": r["show"], "guids": []})
        if r["guid"]:
            f["guids"].append(r["guid"])
    return list(feeds.values())


# ── Settings (admin-managed key/value) ────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(key: str, value: str):
    with _conn() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ── Events / stats ────────────────────────────────────────────────────────────

def log_event(uid: int | None, etype: str):
    with _conn() as c:
        c.execute("INSERT INTO events (user_id, type, created_at) VALUES (?,?,?)",
                  (uid, etype, time.time()))


def stats() -> dict:
    with _conn() as c:
        totals = {r["type"]: r["n"] for r in c.execute(
            "SELECT type, COUNT(*) AS n FROM events GROUP BY type").fetchall()}
        since = time.time() - 7 * 86400
        week = {r["type"]: r["n"] for r in c.execute(
            "SELECT type, COUNT(*) AS n FROM events WHERE created_at>=? GROUP BY type",
            (since,)).fetchall()}
        per_user = [dict(r) for r in c.execute(
            "SELECT u.username, u.last_ip, u.blocked, u.is_admin,"
            " (SELECT COUNT(*) FROM events e WHERE e.user_id=u.id AND e.type='download') AS downloads,"
            " (SELECT COUNT(*) FROM events e WHERE e.user_id=u.id AND e.type='transcript') AS transcripts,"
            " u.id AS id, u.created_at AS created_at"
            " FROM users u ORDER BY u.created_at").fetchall()]
    return {"totals": totals, "week": week, "users": per_user}


# ── Password hashing (stdlib pbkdf2) ──────────────────────────────────────────

# OWASP's current figure for PBKDF2-HMAC-SHA256. Hashes made with the old
# count carry no prefix and are still accepted; they are rewritten with the
# new count at the next successful login, when the password is at hand.
PBKDF2_ROUNDS = 600_000
_LEGACY_ROUNDS = 120_000


def _hash(password: str, salt: str, rounds: int = PBKDF2_ROUNDS) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), rounds).hex()
    return digest if rounds == _LEGACY_ROUNDS else f"p{rounds}${digest}"


def _check(password: str, salt: str, stored: str) -> bool:
    try:
        if stored.startswith("p") and "$" in stored:
            rounds = int(stored[1:stored.index("$")])
        else:
            rounds = _LEGACY_ROUNDS
        if not 1_000 <= rounds <= 10_000_000:
            return False
        return secrets.compare_digest(_hash(password, salt, rounds), stored)
    except ValueError:
        return False


_DUMMY_SALT = secrets.token_hex(16)


def _row_to_user(r: sqlite3.Row) -> dict:
    d = dict(r)
    d.pop("pw_hash", None)
    d.pop("salt", None)
    return d


# ── Users ─────────────────────────────────────────────────────────────────────

def user_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username: str, password: str) -> dict | None:
    username = username.strip()
    if not username or not password:
        return None
    salt = secrets.token_hex(16)
    token = secrets.token_urlsafe(24)
    is_admin = 1 if user_count() == 0 else 0     # first account = admin
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO users (username, pw_hash, salt, api_token, is_admin, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (username, _hash(password, salt), salt, token, is_admin, time.time()),
            )
            uid = cur.lastrowid
        return get_user(uid)
    except sqlite3.IntegrityError:
        return None                              # username taken


def verify_user(username: str, password: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not r:
        # Same work as a real check, so the answer time does not tell
        # whether the name exists.
        _hash(password, _DUMMY_SALT)
        return None
    # Constant-time compare so response timing can't leak hash prefixes.
    if not _check(password, r["salt"], r["pw_hash"]):
        return None
    if not r["pw_hash"].startswith(f"p{PBKDF2_ROUNDS}$"):
        with _conn() as c:
            c.execute("UPDATE users SET pw_hash=? WHERE id=?",
                      (_hash(password, r["salt"]), r["id"]))
    return _row_to_user(r)


def get_user(uid: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return _row_to_user(r) if r else None


def get_user_by_token(token: str) -> dict | None:
    if not token:
        return None
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE api_token=?", (token,)).fetchone()
    return _row_to_user(r) if r else None


def rotate_api_token(uid: int) -> str | None:
    """Replace a user's MCP token and return the new one.

    The token sits in assistant configs on several machines; once one of
    them is lost or shared, the only fix is a new token that the old copies
    no longer match. The old value stops working with this UPDATE.
    """
    token = secrets.token_urlsafe(24)
    with _conn() as c:
        cur = c.execute("UPDATE users SET api_token=? WHERE id=?", (token, uid))
    return token if cur.rowcount else None


def touch_ip(uid: int, ip: str):
    with _conn() as c:
        c.execute("UPDATE users SET last_ip=? WHERE id=?", (ip, uid))


def list_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    return [_row_to_user(r) for r in rows]


def set_blocked(uid: int, blocked: bool):
    with _conn() as c:
        c.execute("UPDATE users SET blocked=? WHERE id=?", (1 if blocked else 0, uid))


def delete_user(uid: int):
    with _conn() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))


# ── IP blocking ───────────────────────────────────────────────────────────────

def block_ip(ip: str):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO blocked_ips (ip, created_at) VALUES (?,?)", (ip, time.time()))


def unblock_ip(ip: str):
    with _conn() as c:
        c.execute("DELETE FROM blocked_ips WHERE ip=?", (ip,))


def is_ip_blocked(ip: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM blocked_ips WHERE ip=?", (ip,)).fetchone() is not None


def list_blocked_ips() -> list[str]:
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT ip FROM blocked_ips ORDER BY created_at").fetchall()]


# ── Per-user Instagram cookies ────────────────────────────────────────────────

COOKIE_COLUMNS = {"instagram": "ig_cookies", "youtube": "yt_cookies"}


def _cookie_column(kind: str) -> str:
    col = COOKIE_COLUMNS.get(kind)
    if not col:
        raise ValueError(f"Unbekannte Cookie-Art: {kind}")
    return col


def set_cookies(uid: int, kind: str, text: str):
    col = _cookie_column(kind)
    with _conn() as c:
        c.execute(f"UPDATE users SET {col}=? WHERE id=?", (text or "", uid))


def get_cookies(uid: int, kind: str) -> str:
    col = _cookie_column(kind)
    with _conn() as c:
        r = c.execute(f"SELECT {col} FROM users WHERE id=?", (uid,)).fetchone()
    return (r[0] if r else "") or ""


# Backwards-compatible aliases — Instagram was the only store originally.
def set_ig_cookies(uid: int, text: str):
    set_cookies(uid, "instagram", text)


def get_ig_cookies(uid: int) -> str:
    return get_cookies(uid, "instagram")
