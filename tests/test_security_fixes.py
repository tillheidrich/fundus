"""Behaviour tests for the security review findings (option injection, SSRF
through redirects, script paths, throttles, headers, token rotation, file
names, upload ordering) and for two paths that had no request-level test:
the Markdown export in both languages and an authenticated MCP initialize.

Each test drives the code path itself; none of them greps source text.
"""
import asyncio
import re
import socket
import uuid

import pytest

import main
import mcp_tools
import podcast

EVIL = "--config-locations=/etc/x"


# ── 1. Option injection via MCP ───────────────────────────────────────────────

@pytest.fixture
def no_spawn(monkeypatch):
    """Record every attempt to start a process instead of starting it."""
    spawned = []

    async def fake_exec(*argv, **kw):
        spawned.append(argv)
        raise AssertionError(f"process started: {argv}")

    async def fake_run_cmd(cmd):
        spawned.append(tuple(cmd))
        raise AssertionError(f"process started: {cmd}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(main, "run_cmd", fake_run_cmd)
    return spawned


@pytest.mark.parametrize("call", [
    lambda: mcp_tools.get_transcript(EVIL),
    lambda: mcp_tools.list_transcript_languages(EVIL),
    lambda: mcp_tools.get_caption(EVIL),
    lambda: mcp_tools.get_metadata([EVIL]),
    lambda: mcp_tools.get_metadata(["https://example.com/ok", EVIL]),
    lambda: mcp_tools.get_download_script([EVIL]),
    lambda: mcp_tools.get_livestream_script(EVIL),
])
def test_mcp_tools_refuse_option_urls_without_spawning(no_spawn, call):
    out = asyncio.run(call())
    assert out.get("error"), out
    assert no_spawn == []


def test_ytdlp_never_sees_the_url_as_an_option(monkeypatch):
    """Defence in depth: even a URL that got past validation sits behind
    "--", where yt-dlp no longer parses options."""
    seen = []

    async def fake_run_cmd(cmd):
        seen.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(main, "run_cmd", fake_run_cmd)
    url = "https://www.youtube.com/watch?v=abc"
    asyncio.run(main._fetch_light_meta(url))
    asyncio.run(main._fetch_caption(url))
    asyncio.run(main._fetch_subs(f"t_{uuid.uuid4().hex}", url, "en"))
    asyncio.run(mcp_tools.list_transcript_languages(url))
    assert len(seen) == 4
    for cmd in seen:
        assert cmd[-2:] == ["--", url], cmd


def test_list_languages_still_parses_ytdlp_output(monkeypatch):
    """The "--" changes argv, not stdout; the table must still parse."""
    out = ("[info] Available automatic captions for abc:\n"
           "Language Name                     Formats\n"
           "de       German                    vtt, json3\n"
           "[info] Available subtitles for abc:\n"
           "Language Name                     Formats\n"
           "en       English                   vtt, json3\n")

    async def fake_run_cmd(cmd):
        return 0, out, ""

    monkeypatch.setattr(main, "run_cmd", fake_run_cmd)
    res = asyncio.run(mcp_tools.list_transcript_languages("https://youtu.be/abc"))
    assert {(l["code"], l["auto"]) for l in res["languages"]} == {("de", True), ("en", False)}


# ── 2. SSRF through redirects ─────────────────────────────────────────────────

FAKE_DNS = {"feed.example": "93.184.216.34", "cdn.example": "151.101.1.1",
            "cdn6.example": "2606:4700::1", "169.254.169.254": "169.254.169.254"}


@pytest.fixture
def fake_dns(monkeypatch):
    def getaddrinfo(host, port, *a, **kw):
        ip = FAKE_DNS[host]
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def _curl(responses):
    """A stand-in for curl: answers from a script and records argv."""
    calls = []

    async def fake_run_cmd(cmd):
        calls.append(cmd)
        status, location, body = responses[len(calls) - 1]
        dest = cmd[cmd.index("-o") + 1]
        with open(dest, "ab") as f:
            f.write(body)
        return 0, f"{status}\n{location}", ""
    return calls, fake_run_cmd


def test_redirect_to_metadata_service_is_refused(fake_dns, monkeypatch, tmp_path):
    calls, fake = _curl([(302, "http://169.254.169.254/latest/meta-data/", b"moved")])
    monkeypatch.setattr(main, "run_cmd", fake)
    dest = tmp_path / "ep.mp3"
    assert asyncio.run(main._fetch_to_file("https://feed.example/ep.mp3", dest)) is False
    assert len(calls) == 1                     # the second hop never ran
    first = calls[0]
    assert "-L" not in first and "-fsSL" not in first
    assert first[first.index("--max-redirs") + 1] == "0"
    assert first[first.index("--resolve") + 1] == "feed.example:443:93.184.216.34"
    assert not dest.exists()


def test_redirects_are_followed_hop_by_hop_and_pinned(fake_dns, monkeypatch, tmp_path):
    calls, fake = _curl([(301, "https://cdn.example/a.mp3", b"x"),
                         (302, "https://cdn6.example:8443/b.mp3", b"y"),
                         (200, "", b"A" * 4096)])
    monkeypatch.setattr(main, "run_cmd", fake)
    dest = tmp_path / "ep.mp3"
    assert asyncio.run(main._fetch_to_file("https://feed.example/ep.mp3", dest)) is True
    pins = [c[c.index("--resolve") + 1] for c in calls]
    assert pins == ["feed.example:443:93.184.216.34", "cdn.example:443:151.101.1.1",
                    "cdn6.example:8443:[2606:4700::1]"]
    assert [c[-1] for c in calls] == ["https://feed.example/ep.mp3", "https://cdn.example/a.mp3",
                                      "https://cdn6.example:8443/b.mp3"]
    # Redirect bodies are not part of the file.
    assert dest.read_bytes() == b"A" * 4096
    # Size and time limits stay on every hop.
    assert all("--max-filesize" in c and "--max-time" in c for c in calls)


def test_endless_redirects_give_up(fake_dns, monkeypatch, tmp_path):
    calls, fake = _curl([(302, "https://cdn.example/again", b"")] * 10)
    monkeypatch.setattr(main, "run_cmd", fake)
    assert asyncio.run(main._fetch_to_file("https://feed.example/x", tmp_path / "x")) is False
    assert len(calls) == 5


# ── 3. Paths in generated scripts ─────────────────────────────────────────────

@pytest.mark.parametrize("evil", ['~/x"; rm -rf ~; echo "', "~/$(id)", "~/`id`",
                                  "~/a;b", "~/a|b", "~/a&b", "~/$HOME"])
def test_malicious_paths_fall_back_to_the_default(evil):
    assert main.shell_path(evil) == "$HOME/Downloads"
    for script in (main.build_whisper_script(evil), main.build_merge_script(evil),
                   main.build_download_script(["https://example.com/v"], out_dir=evil),
                   main.build_yt_transcript_script("https://example.com/v", out_dir=evil)):
        for bad in ("$(id)", "`id`", "rm -rf", "a;b", "a|b", "a&b", "$HOME\"", "/$HOME"):
            assert bad not in script, (evil, bad)
    steps = main.build_livestream_script("https://example.com/v", "start", path=evil)["steps"]
    assert all("$(id)" not in s["c"] and "rm -rf" not in s["c"] for s in steps)


def test_ordinary_paths_still_work():
    assert main.shell_path("~/Downloads/Meine Clips") == "$HOME/Downloads/Meine Clips"
    assert main.shell_path("/Users/you/Übersicht-2026") == "/Users/you/Übersicht-2026"
    assert main.shell_path("~") == "$HOME"
    assert main._safe_base("~/Desktop") == "~/Desktop"
    assert main._safe_base('~/x"$(id)"') == "~/Downloads"


# ── 4. Throttles ──────────────────────────────────────────────────────────────

@pytest.fixture
def one_per_hour(monkeypatch):
    monkeypatch.setattr(main, "BATCH_MAX_PER_HOUR", 1)

    async def nothing(*a, **kw):
        return None
    for name in ("_transcript_job", "_trim_prepare", "_podcast_job"):
        monkeypatch.setattr(main, name, nothing)


def test_transcript_is_throttled(as_user, alice, one_per_hour):
    c = as_user(alice)
    assert c.post("/api/transcript", data={"url": "https://example.com/a"}).status_code == 200
    assert c.post("/api/transcript", data={"url": "https://example.com/b"}).status_code == 429


def test_trim_is_throttled(as_user, alice, one_per_hour):
    c = as_user(alice)
    assert c.post("/api/trim", data={"url": "https://example.com/a"}).status_code == 200
    assert c.post("/api/trim", data={"url": "https://example.com/b"}).status_code == 429


def test_podcast_counts_each_episode(as_user, alice, one_per_hour):
    r = as_user(alice).post("/api/podcast", data={
        "urls": "https://example.com/feed/ep1\nhttps://example.com/feed/ep2"})
    assert r.status_code == 429, r.text


def test_mcp_jobs_share_the_budget(alice, monkeypatch, no_spawn):
    monkeypatch.setattr(main, "BATCH_MAX_PER_HOUR", 0)
    tok = mcp_tools.current_mcp_user.set(alice)
    try:
        for call in (mcp_tools.get_transcript("https://example.com/a"),
                     mcp_tools.get_metadata(["https://example.com/a"]),
                     mcp_tools.get_podcast_package(["https://example.com/ep"])):
            out = asyncio.run(call)
            assert "Limit" in out.get("error", ""), out
    finally:
        mcp_tools.current_mcp_user.reset(tok)
    assert no_spawn == []


def test_mcp_metadata_is_capped(monkeypatch, no_spawn):
    monkeypatch.setattr(main, "BATCH_MAX_URLS", 2)
    out = asyncio.run(mcp_tools.get_metadata([f"https://example.com/{i}" for i in range(3)]))
    assert "höchstens 2" in out["error"]
    assert no_spawn == []


# ── 6. Security headers ───────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [("get", "/health"), ("get", "/api/me"),
                                         ("post", "/mcp")])
def test_security_headers_on_every_response(client, method, path):
    r = getattr(client, method)(path)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert "script-src" not in r.headers["content-security-policy"]


# ── 7. Token rotation ─────────────────────────────────────────────────────────

def test_token_rotation_invalidates_the_old_token(as_user, alice):
    c = as_user(alice)
    old = c.get("/api/me").json()["api_token"]
    r = c.post("/api/me/token/rotate")
    assert r.status_code == 200
    new = r.json()["api_token"]
    assert new and new != old
    assert c.get("/api/me").json()["api_token"] == new
    assert main.db.get_user_by_token(old) is None
    assert main.db.get_user_by_token(new)["id"] == alice["id"]
    # The old token no longer opens /mcp.
    r = c.post("/mcp", headers={"Authorization": f"Bearer {old}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401


def test_token_rotation_needs_a_session(client):
    client.cookies.clear()
    assert client.post("/api/me/token/rotate").status_code == 401


# ── 8. Content-Disposition ────────────────────────────────────────────────────

def _check_disposition(value: str, real_name: str):
    value.encode("latin-1")                    # must be sendable as a header
    m = re.fullmatch(r'attachment; filename="([^"]*)"; filename\*=UTF-8\'\'(\S+)', value)
    assert m, value
    fallback, encoded = m.groups()
    assert fallback.isascii() and fallback
    from urllib.parse import unquote
    assert unquote(encoded) == real_name


def test_content_disposition_handles_umlauts_emoji_and_quotes():
    name = 'Grüße 🎉 "Zitat".md'
    _check_disposition(main.content_disposition(name), name)


def _done_job(user, title="Wissensarbeit"):
    jid = str(uuid.uuid4())
    main.jobs[jid] = {
        "status": "done", "user_id": user["id"], "type": "transcript",
        "url": "https://example.com/v",
        "segments": [{"start": 0.0, "dur": 2.0, "text": "Moin."},
                     {"start": 65.0, "dur": 3.0, "text": "Jetzt geht es um Preise."}],
        "transcript": "Moin. Jetzt geht es um Preise.", "used_lang": "de",
        "word_count": 6,
        "meta": {"title": title, "webpage_url": "https://example.com/v",
                 "uploader": "Werkstattfunk", "upload_date": "20261001"},
    }
    return jid


def test_export_filename_with_umlauts_emoji_and_quote(as_user, alice):
    title = 'Grüße 🎉 "Zitat"'
    jid = _done_job(alice, title)
    try:
        r = as_user(alice).get(f"/api/transcript/{jid}/export.md",
                               params={"filename": "[title]"})
        assert r.status_code == 200, r.text
        cd = r.headers["content-disposition"]
        assert "filename*=UTF-8''" in cd and "Gr%C3%BC%C3%9Fe" in cd and "%F0%9F%8E%89" in cd
        assert cd.count('"') == 2               # only the fallback's own quotes
    finally:
        main.jobs.pop(jid, None)


# ── 9. Local token compare ────────────────────────────────────────────────────

def _post_request(headers: dict):
    from starlette.requests import Request
    return Request({"type": "http", "method": "POST", "path": "/api/x", "query_string": b"",
                    "scheme": "http", "server": ("127.0.0.1", 8765),
                    "client": ("127.0.0.1", 1234),
                    "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})


def test_local_request_token(monkeypatch):
    monkeypatch.setattr(main, "LOCAL_TOKEN", "s3cret")
    foreign = {"host": "127.0.0.1:8765", "origin": "https://evil.example"}
    assert main._local_request_allowed(_post_request({**foreign, "x-fundus-token": "s3cret"}))
    assert not main._local_request_allowed(_post_request({**foreign, "x-fundus-token": "s3cre"}))
    assert not main._local_request_allowed(_post_request(foreign))


# ── 10. Podcast transcript upload ─────────────────────────────────────────────

@pytest.fixture
def package_with_transcript(alice):
    from tests.test_upload_transcribe import EP
    pid = str(uuid.uuid4())
    ep = dict(EP, title="Bestandsfolge")
    slug = podcast.episode_slug(ep)
    folder = main._podcast_dir(pid) / slug
    folder.mkdir(parents=True)
    old = folder / f"{slug}_Transkript.txt"
    old.write_text("das alte Transkript", encoding="utf-8")
    main.podcasts[pid] = {"status": "done", "user_id": alice["id"], "episodes": [ep]}
    yield pid, slug, old
    main.podcasts.pop(pid, None)


@pytest.mark.parametrize("name,body", [("x.vtt", b"WEBVTT\n\nkeine Zeitmarken hier\n"),
                                       ("x.srt", b"nur Text, keine Zeiten"),
                                       ("x.txt", b"   \n\n "), ("x.txt", b"")])
def test_broken_upload_leaves_existing_transcript(as_user, alice, package_with_transcript,
                                                  name, body):
    pid, slug, old = package_with_transcript
    r = as_user(alice).post(f"/api/podcast/{pid}/transcript/{slug}",
                            files={"file": (name, body)})
    assert r.status_code == 400, r.text
    assert old.read_text(encoding="utf-8") == "das alte Transkript"


def test_valid_upload_replaces_and_labels_in_english(as_user, alice, package_with_transcript):
    from tests.test_upload_transcribe import VTT
    pid, slug, old = package_with_transcript
    r = as_user(alice).post(f"/api/podcast/{pid}/transcript/{slug}",
                            files={"file": (f"{slug}.vtt", VTT.encode())})
    assert r.status_code == 200, r.text
    assert "Zettelkasten" in old.read_text(encoding="utf-8")
    md = (old.parent / f"{slug}_Transkript.md").read_text(encoding="utf-8")
    assert "**Transcript source:** Whisper script, local" in md


# ── 13. Markdown export in both languages ─────────────────────────────────────

@pytest.mark.parametrize("how,lang,labels", [
    ("query", "de", ("**Quelle:**", "**Datum:**", "**Sprache:**", "## Transkript")),
    ("query", "en", ("**Source:**", "**Date:**", "**Language:**", "## Transcript")),
    ("cookie", "en", ("**Source:**", "**Date:**", "**Language:**", "## Transcript")),
    ("cookie", "de", ("**Quelle:**", "**Datum:**", "**Sprache:**", "## Transkript")),
])
def test_markdown_export_labels_follow_the_language(as_user, alice, how, lang, labels):
    jid = _done_job(alice)
    c = as_user(alice)
    try:
        if how == "cookie":
            c.cookies.set(main.i18n.COOKIE_NAME, lang)
            r = c.get(f"/api/transcript/{jid}/export.md")
        else:
            r = c.get(f"/api/transcript/{jid}/export.md", params={"lang": lang})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/markdown")
        for label in labels:
            assert label in r.text, (label, r.text)
        assert "[1:05] Jetzt geht es um Preise." in r.text
    finally:
        c.cookies.delete(main.i18n.COOKIE_NAME)
        main.jobs.pop(jid, None)


# ── 13. MCP over HTTP with a real token ───────────────────────────────────────

def test_mcp_initialize_with_bearer_token(alice):
    """The whole path a client takes: middleware token check, the /mcp
    rewrite, the mounted app, and a JSON (not SSE) answer.

    The session manager is a fresh one: the application's own can only be
    started once per process, and the test client does not run the
    lifespan that would start it."""
    import httpx
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    token = main.db.get_user(alice["id"])["api_token"]
    fresh = StreamableHTTPSessionManager(app=mcp_tools.mcp._mcp_server,
                                         json_response=True, stateless=True)

    async def run():
        old = mcp_tools.mcp._session_manager
        mcp_tools.mcp._session_manager = fresh
        try:
            async with fresh.run():
                transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 5555))
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://127.0.0.1:8765") as c:
                    return await c.post("/mcp", headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26",
                                         "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "1"}}})
        finally:
            mcp_tools.mcp._session_manager = old

    main.db.init_db()
    r = asyncio.run(run())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"] == "fundus"
    assert r.headers["x-content-type-options"] == "nosniff"
