"""Desktop history: recorded only on the Mac, deletable, silent in private mode."""
import asyncio

import pytest

import main


@pytest.fixture
def desktop(monkeypatch, client):
    monkeypatch.setattr(main, "DESKTOP_MODE", True)
    main.db.clear_history()
    main.db.set_setting("private_mode", "0")
    yield
    main.db.clear_history()
    main.db.set_setting("private_mode", "0")


async def _noop():
    return None


def _remember(*entries):
    asyncio.run(main._remembered(_noop(), lambda: list(entries)))


def test_server_records_nothing_and_hides_the_endpoint(monkeypatch, as_user, alice):
    monkeypatch.setattr(main, "DESKTOP_MODE", False)
    main.db.clear_history()
    _remember(("media", "https://example.com/a", "A"))
    assert main.db.list_history() == []
    assert as_user(alice).get("/api/history").status_code == 404


def test_desktop_records_lists_and_deletes(desktop, as_user, alice):
    _remember(("media", "https://example.com/a", "A"), ("transcript", "https://example.com/b", "B"))
    c = as_user(alice)
    items = c.get("/api/history").json()["items"]
    assert [i["title"] for i in items] == ["B", "A"]
    assert c.delete(f"/api/history/{items[0]['id']}").json()["deleted"] == 1
    assert [i["title"] for i in c.get("/api/history").json()["items"]] == ["A"]
    c.delete("/api/history")
    assert c.get("/api/history").json()["items"] == []


def test_private_mode_records_nothing(desktop, as_user, alice):
    c = as_user(alice)
    assert c.post("/api/history/private", data={"on": "true"}).json()["private"] is True
    _remember(("media", "https://example.com/a", "A"))
    d = c.get("/api/history").json()
    assert d["private"] is True and d["items"] == []


def test_failed_jobs_are_not_listed(desktop):
    async def boom():
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        asyncio.run(main._remembered(boom(), lambda: []))
    assert main.db.list_history() == []


FEED = b"""<?xml version="1.0"?><rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
<title>Werkstattfunk</title>
<item><guid>g3</guid><title>Drei</title><pubDate>Wed, 01 Oct 2026 08:00:00 GMT</pubDate></item>
<item><guid>g2</guid><title>Zwei</title><pubDate>Wed, 24 Sep 2026 08:00:00 GMT</pubDate></item>
<item><guid>g1</guid><title>Eins</title><pubDate>Wed, 17 Sep 2026 08:00:00 GMT</pubDate></item>
<item><guid>g0</guid><title>Null</title><pubDate>Wed, 10 Sep 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_new_episodes_are_only_those_after_the_newest_fetched(desktop, monkeypatch, as_user, alice):
    async def fake_fetch(url, timeout=20, as_json=False, **kw):
        return FEED
    monkeypatch.setattr(main.podcast, "fetch", fake_fetch)
    _remember(("podcast", "https://example.com/e1", "Eins", "Werkstattfunk",
               "https://feeds.example.com/w.xml", "g1", "Wed, 17 Sep 2026 08:00:00 GMT"))
    d = as_user(alice).get("/api/history/new-episodes").json()
    eps = d["feeds"][0]["episodes"]
    # g0 is older than what was fetched: back catalogue, not "new".
    assert [e["guid"] for e in eps] == ["g3", "g2"]
