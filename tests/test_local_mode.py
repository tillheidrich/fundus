"""Local (Mac app) versus server operation.

The same code runs in both places. What differs is not preference but
circumstance: a datacenter IP is treated as suspicious by YouTube and a home
connection is not, and a laptop has disk space and a Neural Engine where a
4 vCPU VPS has neither. Everything here follows from those two facts.

The defaults are set with setdefault, so an explicit environment variable
always wins — that property is what these tests mostly guard, because losing
it would make a local instance unconfigurable.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import inspect

import pytest

import main

ROOT = Path(__file__).resolve().parent.parent


def code_only(src: str) -> str:
    """Strip comments before asserting on source.

    Without this, a test that forbids a term also forbids explaining why the
    term is absent — and the comment explaining a removal is exactly where
    that term belongs. Both of these tests failed on their own documentation
    the first time round.
    """
    out = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("//", "#", "*", '"""')):
            continue
        out.append(line.split("//")[0] if "://" not in line else line)
    return "\n".join(out)


def probe(env: dict, expr: str) -> str:
    """Import main in a fresh interpreter — the constants are read at import
    time, so monkeypatching the environment afterwards proves nothing.

    The parent environment is inherited rather than replaced: a bare env would
    hide the interpreter's own package path and every probe would fail on the
    import instead of testing anything. The keys that matter are cleared
    explicitly below so a developer's own settings cannot leak into a result.
    """
    import os
    base = {k: v for k, v in os.environ.items()
            if k not in ("LOCAL_MODE", "WHISPER_MODEL", "CLEAN_AGE_HOURS",
                         "BATCH_MAX_URLS", "BROWSER_COOKIES",
                         "ENABLE_MEDIA_TOOLS")}
    code = f"import main; print({expr})"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**base, **env},
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stderr[-800:]
    return r.stdout.strip()


# ── The switch itself ─────────────────────────────────────────────────────────

def test_server_is_the_default():
    """An instance that does not say otherwise is a server. Getting this
    backwards would quietly disable the guardrails on a small VPS."""
    assert probe({}, "main.LOCAL_MODE") == "False"


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
def test_local_mode_accepts_the_usual_spellings(value):
    assert probe({"LOCAL_MODE": value}, "main.LOCAL_MODE") == "True"


@pytest.mark.parametrize("key,server,local", [
    ("CLEAN_AGE_H", "6", "168"),           # disk is not scarce on a laptop
    ("WHISPER_MODEL", "base", "large-v3-turbo"),
    ("BATCH_MAX_URLS", "150", "500"),
])
def test_defaults_differ_by_where_we_run(key, server, local):
    assert probe({}, f"main.{key}") == server
    assert probe({"LOCAL_MODE": "1"}, f"main.{key}") == local


def test_explicit_environment_beats_the_local_default():
    """setdefault, not assignment. Without this a local instance could not be
    configured at all."""
    assert probe({"LOCAL_MODE": "1", "WHISPER_MODEL": "small"},
                 "main.WHISPER_MODEL") == "small"
    assert probe({"LOCAL_MODE": "1", "CLEAN_AGE_HOURS": "2"},
                 "main.CLEAN_AGE_H") == "2"


# ── Browser cookies ───────────────────────────────────────────────────────────

def test_browser_cookies_are_off_unless_asked_for():
    assert probe({}, "repr(main.BROWSER_COOKIES)") == "''"


def test_known_browser_is_accepted():
    assert probe({"BROWSER_COOKIES": "safari"},
                 "main._browser_cookie_args()") == "['--cookies-from-browser', 'safari']"


@pytest.mark.parametrize("attack", [
    "safari; rm -rf /", "safari && curl evil.sh", "../../etc/passwd", "$(whoami)",
])
def test_browser_name_cannot_carry_a_payload(attack):
    """It goes straight into an argument list. An allowlist is the only check
    that does not need to be right about shell quoting."""
    assert probe({"BROWSER_COOKIES": attack}, "repr(main.BROWSER_COOKIES)") == "''"


def test_uploaded_cookies_win_over_the_browser_jar(monkeypatch):
    """Handing yt-dlp both sources is ambiguous, and an uploaded file is the
    more deliberate of the two."""
    monkeypatch.setattr(main, "BROWSER_COOKIES", "safari")
    assert "--cookies-from-browser" not in main._yt_extractor_args(True)
    assert "--cookies-from-browser" in main._yt_extractor_args(False)


def test_selftest_deliberately_refuses_the_browser_jar():
    """The check has to answer 'is this IP welcome', not 'can we get in
    somehow'. With cookies quietly attached it would report OK on a blocked
    address — the one answer that sends you looking in the wrong place."""
    src = __import__("inspect").getsource(main.admin_selftest)
    assert "_yt_extractor_args(True)" in src
    assert "_yt_extractor_args()" not in src


# ── Neural Engine transcription ───────────────────────────────────────────────

def test_mlx_is_local_only():
    """Docker on macOS has no access to the ANE, and a Linux server has none
    at all — trying would just cost an import and a traceback."""
    src = __import__("inspect").getsource(main._mlx_available)
    assert "if not LOCAL_MODE" in src


def test_mlx_failure_falls_back_rather_than_failing():
    """A missing model download should cost quality, not the transcript."""
    src = __import__("inspect").getsource(main._whisper_segments)
    assert "_run_mlx" in src and "_run" in src
    assert "log_error" in src


def test_every_offered_model_has_an_mlx_repo():
    for name, repo in main._MLX_REPOS.items():
        assert repo.startswith("mlx-community/"), name


def test_cpu_path_survives_an_mlx_model_name():
    """large-v3-turbo is the local default and faster-whisper does not know
    it; loading must fall back to a size it recognises instead of raising."""
    src = __import__("inspect").getsource(main._load_whisper)
    assert '"large-v3"' in src and "else \"base\"" in src


# ── The Mac app wrapper ───────────────────────────────────────────────────────

MACOS = ROOT / "macos"


def test_app_sources_exist():
    assert (MACOS / "Package.swift").is_file()
    assert (MACOS / "Sources/FundusApp/main.swift").is_file()
    assert (MACOS / "Sources/FundusApp/Backend.swift").is_file()
    assert (MACOS / "build-app.sh").is_file()


def test_app_runs_the_server_in_local_mode():
    src = (MACOS / "Sources/FundusApp/Backend.swift").read_text()
    assert 'env["LOCAL_MODE"] = "1"' in src


def test_app_keeps_user_data_outside_the_bundle():
    """An update replaces the .app. Taking the accounts with it would be rude."""
    src = (MACOS / "Sources/FundusApp/Backend.swift").read_text()
    for key in ("DATA_DIR", "DOWNLOAD_DIR", "TMP_DIR", "WHISPER_MODEL_DIR"):
        assert f'env["{key}"]' in src


def test_the_server_actually_honours_those_variables():
    """The half the test above cannot see, and the half that was broken.

    Swift passed all three faithfully; `main.py` and `db.py` opened with
    `Path("downloads")` and ignored them. So the app wrote downloads and the
    user database *into its own bundle* — unwritable in /Applications,
    discarded by the next update, and enough to invalidate the signature.
    Checking that the variable is set says nothing about whether anyone reads
    it; this runs the assignment and looks at where it points.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe = (
            "import os, main, db;"
            "print(main.DOWNLOAD_DIR);"
            "print(main.TMP_DIR);"
            "print(main.DATA_DIR);"
            "print(db.DB_PATH)"
        )
        env = {**os.environ,
               "DOWNLOAD_DIR": f"{tmp}/dl", "TMP_DIR": f"{tmp}/t",
               "DATA_DIR": f"{tmp}/d", "LOCAL_MODE": "1"}
        out = subprocess.run([sys.executable, "-c", probe], env=env,
                             capture_output=True, text=True,
                             cwd=str(ROOT)).stdout
        lines = out.strip().splitlines()[-4:]
        assert len(lines) == 4, out
        for line in lines:
            assert line.startswith(tmp), f"liegt ausserhalb: {line}"


def test_app_picks_a_free_port():
    """A fixed port collides with whatever else this user is running, and
    this app is for people who run things."""
    src = (MACOS / "Sources/FundusApp/Backend.swift").read_text()
    assert "freePort" in src


def test_app_shuts_the_server_down_gently():
    """SIGTERM lets uvicorn wind down; a hard kill orphans running ffmpeg."""
    src = (MACOS / "Sources/FundusApp/Backend.swift").read_text()
    assert "SIGTERM" in src


def test_build_script_excludes_developer_state():
    """Shipping data/ would ship the developer's account database."""
    src = (MACOS / "build-app.sh").read_text()
    assert "rm -rf" in src and "$SRV/data" in src


def test_external_links_leave_the_app_window():
    """The page links to YouTube and an extension store; opening those inside
    an app window is confusing and pointless."""
    src = (MACOS / "Sources/FundusApp/main.swift").read_text()
    assert "NSWorkspace.shared.open" in src


def test_app_repairs_the_path_before_starting_the_server():
    """An app launched from the Dock inherits launchd's minimal PATH, with no
    Homebrew in it — so ffmpeg, ffprobe, yt-dlp and the JS runtime are all
    missing. Launched from a terminal it inherits the shell's PATH and works,
    which is exactly why this is easy to ship broken."""
    src = (MACOS / "Sources/FundusApp/Backend.swift").read_text()
    assert "/opt/homebrew/bin" in src
    assert 'env["PATH"]' in src


def test_window_can_be_moved():
    """fullSizeContentView puts the web view on top of the only drag area the
    window has, and it swallows the events — the window cannot be moved at
    all. Found by using it, which is the only way this one shows up."""
    src = code_only((MACOS / "Sources/FundusApp/main.swift").read_text())
    assert ".fullSizeContentView" not in src
    assert "isMovableByWindowBackground" in src


# ── No login on the desktop ───────────────────────────────────────────────────

def test_login_is_skipped_only_locally_and_only_from_this_machine():
    """A password on a server bound to 127.0.0.1 protects nobody — anything
    that can reach the port already runs as this user. But the exemption has
    to be both conditions: LOCAL_MODE alone would open up an instance that
    someone binds to 0.0.0.0."""
    src = __import__("inspect").getsource(main._gate)
    # DESKTOP_MODE statt LOCAL_MODE: die Bedingung wurde enger, die Aussage
    # des Tests bleibt — beide Teile müssen zusammenkommen, LOCAL_MODE allein
    # würde eine Instanz öffnen, die jemand auf 0.0.0.0 bindet.
    assert "_desktop_request(request)" in src
    dsrc = __import__("inspect").getsource(main._desktop_request)
    for part in ("DESKTOP_MODE", "_is_loopback", "_host_is_local", "_has_local_token"):
        assert part in dsrc, part


def test_loopback_check_ignores_forwarded_headers():
    """X-Forwarded-For is attacker-controlled. Trusting it here would let
    anyone claim to be local and walk straight past the login."""
    import ast, inspect
    src = inspect.getsource(main._is_loopback).strip()
    fn = ast.parse(src).body[0]
    # Every statement except the docstring — the docstring legitimately says
    # "forwarded" while explaining why no header is read, and a plain text
    # search cannot tell the explanation from the thing it warns against.
    stmts = [s for s in fn.body
             if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                     and isinstance(s.value.value, str))]
    body = "\n".join(ast.get_source_segment(src, s) or "" for s in stmts)
    assert "request.client" in body
    assert "headers" not in body


def test_server_mode_still_requires_a_login():
    assert probe({}, "main.LOCAL_MODE") == "False"


def test_local_account_gets_an_unguessable_password():
    """It is never typed, but it is a real account row — a fixed or empty
    password would become a real hole the moment someone exposes the port."""
    src = __import__("inspect").getsource(main._local_user)
    assert "secrets.token_urlsafe" in src


# ── Component overview ────────────────────────────────────────────────────────

def test_overview_reports_the_js_runtime():
    """The single most misdiagnosed dependency: without it YouTube's
    n-challenge is unsolvable and the symptom looks like an IP ban."""
    src = __import__("inspect").getsource(main._component_versions)
    assert "js_runtime" in src


def test_overview_distinguishes_the_whisper_backend():
    """A silent fall back from Neural Engine to CPU just feels slow."""
    src = __import__("inspect").getsource(main._component_versions)
    assert "mlx" in src and "CPU" in src


def test_overview_is_cached():
    """It shells out several times; the settings page must not pay that on
    every visit."""
    src = __import__("inspect").getsource(main._component_versions)
    assert "if _components:" in src


def test_update_invalidates_the_overview():
    """Otherwise it keeps reporting the version from before the update."""
    assert "_components.clear()" in __import__("inspect").getsource(main._self_update)


def test_self_update_uses_its_own_interpreter():
    """A bare `pip` resolves to whichever is first on PATH — in the Mac app
    that is the system one, and the update would land where the server never
    looks."""
    src = __import__("inspect").getsource(main._self_update)
    assert "sys.executable" in src


# ── App updates ───────────────────────────────────────────────────────────────

UPDATER = MACOS / "Sources/FundusApp/Updater.swift"


def test_updater_exists():
    assert UPDATER.is_file()


def test_update_check_is_offered_in_the_menu():
    """A check that only runs on launch is one the user cannot repeat when
    they have a reason to."""
    src = (MACOS / "Sources/FundusApp/main.swift").read_text()
    assert "Nach Updates suchen" in src
    assert "checkForUpdates" in src


def test_drafts_and_prereleases_are_ignored():
    """Neither is an offer to the general public."""
    src = UPDATER.read_text()
    assert '"draft"' in src and '"prerelease"' in src


def test_background_check_is_rate_limited():
    """Asking GitHub on every launch is rude and pointless."""
    assert "24 * 3600" in UPDATER.read_text()


def test_a_declined_version_stays_declined():
    """Otherwise 'Later' means 'ask me again in a minute'."""
    assert "SkippedVersion" in UPDATER.read_text()


def test_build_version_is_a_tag_not_a_hash():
    """The check compares against the newest published tag. `git describe
    --always` degrades to a bare commit hash on an untagged repo, and nothing
    sensible compares to that."""
    src = (MACOS / "build-app.sh").read_text()
    assert "--abbrev=0" in src


# ── Der Extraktor-Knopf ───────────────────────────────────────────────────────
#
# Den Endpunkt gab es von Anfang an, aber bis 10/2026 rief ihn nichts auf: in
# der Oberfläche stand die installierte Version und kein Knopf daneben. Diese
# Tests halten fest, wer ihn benutzen darf.
#
# Geprüft wird am Quelltext, nicht über HTTP. Der Grund steht schon weiter
# oben in dieser Datei: FastAPI bindet `Depends(...)` beim Definieren der
# Route: ein Patch auf das Modulattribut kommt zu spät, und ein Test, der das
# nicht weiß, prüft am Ende nur, dass eine Anfrage ohne Sitzung 401 ergibt.

def test_desktop_may_update_without_being_admin():
    """Auf dem Desktop gibt es keine anderen Nutzer, also niemanden zu
    schützen. Hängt der Knopf am Admin-Flag, ist er in einer Datenbank, deren
    erster Account aus irgendeinem Grund kein Administrator ist, unerreichbar
    — und yt-dlp lässt sich auf der eigenen Maschine nicht aktualisieren."""
    src = __import__("inspect").getsource(main.require_maintainer)
    assert "_desktop_request(request)" in src
    # Die Ausnahme muss VOR der Admin-Prüfung stehen, sonst wirkt sie nicht.
    assert src.index("_desktop_request") < src.index("is_admin")


def test_server_still_requires_admin():
    """Die Gegenprobe: ein Update tauscht Code aus, den danach alle Nutzer
    der Instanz ausführen. Das bleibt eine Admin-Entscheidung."""
    src = __import__("inspect").getsource(main.require_maintainer)
    assert 'HTTPException(403' in src and 'is_admin' in src


def test_update_endpoint_is_behind_the_maintainer_check():
    """Der Knopf ist neu, der Endpunkt nicht. Falls jemand beim Umbauen die
    Abhängigkeit verliert, darf das nicht unbemerkt bleiben."""
    src = __import__("inspect").getsource(main.update)
    assert "require_maintainer" in src


def test_version_separates_installed_from_available():
    """Installiert und verfügbar müssen getrennt bleiben. Eine einzige Zahl
    liest sich wie »hat sich selbst aktualisiert«, und das tut es nicht."""
    src = __import__("inspect").getsource(main.version)
    for key in ("latest", "latest_checked", "behind"):
        assert f'"{key}"' in src


# ── Der Login darf nicht an LOCAL_MODE allein hängen ─────────────────────────

def test_login_bypass_needs_more_than_local_mode():
    """Die Loopback-Ausnahme stützt sich auf request.client. Steht auf
    derselben Maschine ein nginx davor, ist das für JEDEN Besucher aus dem
    Internet 127.0.0.1 — und wer LOCAL_MODE=1 in die .env eines öffentlichen
    Containers schreibt, hätte eine Instanz ohne Login, deren impliziter
    Nutzer der erste Account und damit Administrator ist.

    Deshalb hängt der Login an DESKTOP_MODE, nicht an LOCAL_MODE."""
    src = inspect.getsource(main)
    assert "LOCAL_MODE and _is_loopback(request)" not in src, \
        "Der Login hängt wieder an LOCAL_MODE allein"
    assert src.count("_desktop_request(request)") >= 4


def test_desktop_mode_demands_the_app_marker_and_no_proxy():
    # Bis zur SCHLIESSENDEN Klammer der Zuweisung, nicht bis zur ersten:
    # os.environ.get("FUNDUS_DESKTOP", "") enthält selbst eine, und mein
    # erster Versuch schnitt den Ausdruck genau dort ab.
    src = inspect.getsource(main).split("DESKTOP_MODE = (", 1)[1].split("\n)", 1)[0]
    assert "FUNDUS_DESKTOP" in src
    assert "TRUSTED_PROXIES" in src


def test_the_mac_app_sets_the_marker():
    """Sonst sperrt die Prüfung die App aus, für die sie gedacht ist."""
    src = (MACOS / "Sources" / "FundusApp" / "Backend.swift").read_text()
    assert 'env["FUNDUS_DESKTOP"] = "1"' in src
