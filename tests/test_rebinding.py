"""DNS rebinding and desktop access: the address checked is the address used,
and only the app's own WebView counts as "the person at this Mac"."""
import asyncio
import socket

import pytest

import main
import podcast


def test_fetch_pins_the_checked_address(monkeypatch):
    answers = iter(["93.184.216.34", "169.254.169.254"])   # check, then attack

    def fake_gai(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)

    seen = {}
    from curl_cffi import requests as cffi
    from curl_cffi import CurlOpt

    class FakeResp:
        status_code = 200
        headers = {}

    class FakeSession:
        def __init__(self, curl_options=None, **k):
            seen["opts"] = curl_options

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, content_callback=None, **k):
            content_callback(b"ok")
            return FakeResp()
    monkeypatch.setattr(cffi, "Session", FakeSession)
    body = asyncio.run(podcast.fetch("https://feeds.example.com/x.xml"))
    assert body == b"ok"
    assert seen["opts"][CurlOpt.RESOLVE] == ["feeds.example.com:443:93.184.216.34"]


def test_fetch_stops_at_the_size_limit(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p, *a, **k: [(2, 1, 6, "", ("93.184.216.34", p))])
    from curl_cffi import requests as cffi

    class Big:
        def __init__(self, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, url, content_callback=None, **k):
            for _ in range(10):
                content_callback(b"x" * 1000)
    monkeypatch.setattr(cffi, "Session", Big)
    assert asyncio.run(podcast.fetch("https://example.com/big", max_bytes=5000)) is None


def test_preview_parser_is_linear_on_hostile_html():
    import time
    hostile = ("<meta " * 60_000).encode()
    t0 = time.time()
    main._head_meta(hostile)
    assert time.time() - t0 < 2


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setattr(main, "DESKTOP_MODE", True)
    monkeypatch.setattr(main, "LOCAL_TOKEN", "s3cret-token")


def test_foreign_host_header_is_refused(desktop, client):
    """A rebound domain arrives with its own name in Host."""
    r = client.get("/api/history", headers={"host": "evil.example:8765"})
    assert r.status_code == 421


def test_loopback_without_the_token_gets_no_desktop_access(desktop, client):
    """Another account on the same Mac, or any browser tab."""
    assert client.get("/api/history").status_code in (401, 404)
    assert not main._desktop_request(type("R", (), {
        "headers": {"host": "127.0.0.1:8765"}, "cookies": {}, "query_params": {},
        "client": type("C", (), {"host": "127.0.0.1"})()})())


def test_the_app_bootstraps_a_cookie_from_the_first_load(desktop, client):
    r = client.get("/?t=s3cret-token")
    assert r.status_code == 200
    assert "fundus_local=s3cret-token" in r.headers.get("set-cookie", "")
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert client.get("/api/history").status_code == 200        # cookie now carried


def test_the_header_alone_also_works(desktop, client):
    assert client.get("/api/history", headers={"x-fundus-token": "s3cret-token"}).status_code == 200
    assert client.get("/api/history", headers={"x-fundus-token": "wrong"}).status_code in (401, 404)
