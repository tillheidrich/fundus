"""Link preview: oEmbed where offered, Open Graph otherwise, no images."""
import json

import main


def test_youtube_uses_oembed(monkeypatch, as_user, alice):
    calls = []

    async def fake_fetch(url, timeout=20, as_json=False, **kw):
        calls.append(url)
        return json.dumps({"title": "Big Buck Bunny", "author_name": "Blender",
                           "provider_name": "YouTube"}).encode()
    monkeypatch.setattr(main.podcast, "fetch", fake_fetch)
    main._PREVIEW_CACHE.clear()
    d = as_user(alice).get("/api/preview", params={"url": "https://youtu.be/aqz-KE-bpKQ"}).json()
    assert d["title"] == "Big Buck Bunny" and d["author"] == "Blender"
    assert calls[0].startswith("https://www.youtube.com/oembed")
    assert "thumbnail" not in json.dumps(d)


def test_other_pages_use_open_graph(monkeypatch, as_user, alice):
    async def fake_fetch(url, timeout=20, as_json=False, **kw):
        return (b'<html><head><meta property="og:title" content="Folge 12 &amp; mehr">'
                b'<meta property="og:site_name" content="Werkstattfunk"></head></html>')
    monkeypatch.setattr(main.podcast, "fetch", fake_fetch)
    main._PREVIEW_CACHE.clear()
    d = as_user(alice).get("/api/preview", params={"url": "https://example.com/f12"}).json()
    assert d["title"] == "Folge 12 & mehr" and d["site"] == "Werkstattfunk"


def test_lookalike_host_does_not_get_the_oembed_path(monkeypatch, as_user, alice):
    calls = []

    async def fake_fetch(url, timeout=20, as_json=False, **kw):
        calls.append(url)
        return b"<title>x</title>"
    monkeypatch.setattr(main.podcast, "fetch", fake_fetch)
    main._PREVIEW_CACHE.clear()
    as_user(alice).get("/api/preview", params={"url": "https://evil.example/youtube.com/x"})
    assert not any("oembed" in c for c in calls)


def test_non_http_is_refused(as_user, alice):
    r = as_user(alice).get("/api/preview", params={"url": "--exec=id"})
    assert r.status_code == 400
