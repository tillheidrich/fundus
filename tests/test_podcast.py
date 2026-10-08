"""Podcast resolution and packaging.

Most of these guard specific things that went wrong during the two manual runs
the concept was written from (17 episodes, eight shows). Each test names the
failure rather than the behaviour, because the behaviour is obvious and the
reason it exists is not.
"""
import asyncio
import inspect
import json
from datetime import datetime, timezone

import pytest

import main
import podcast

FEED = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>Werkstattfunk</title><language>de</language>
  <itunes:author>Beispielautor</itunes:author>
  <description>Eine Sendung.</description>
  <item>
    <title>Folge 12: Daten im Betrieb | Dr. Anna Beispiel, CTO</title>
    <guid>abc-123</guid>
    <pubDate>Thu, 18 Sep 2026 06:00:00 +0000</pubDate>
    <itunes:duration>39:12</itunes:duration>
    <enclosure url="https://example.com/ep12.m4a" type="audio/x-m4a" length="18200000"/>
    <podcast:transcript url="https://example.com/ep12.vtt" type="text/vtt" language="de"/>
    <content:encoded><![CDATA[<p>Mit <a href="https://example.com/q">Quelle</a>.</p>]]></content:encoded>
  </item>
  <item>
    <title>Folge 11: Etwas anderes</title>
    <guid>abc-122</guid>
    <pubDate>Thu, 11 Sep 2026 06:00:00 +0000</pubDate>
    <itunes:duration>2100</itunes:duration>
    <enclosure url="https://example.com/ep11.mp3" type="audio/mpeg" length="12000000"/>
  </item>
</channel></rss>"""


@pytest.fixture
def parsed():
    return podcast.parse_feed(FEED)


# ── Feed parsing ──────────────────────────────────────────────────────────────

def test_duration_formats_all_parse(parsed):
    """itunes:duration is 'HH:MM:SS', 'MM:SS' or seconds, depending entirely
    on which tool generated the feed. All three appear in the eight shows."""
    assert parsed["episodes"][0]["duration_s"] == 39 * 60 + 12
    assert parsed["episodes"][1]["duration_s"] == 2100


@pytest.mark.parametrize("raw,expected", [
    ("39:12", 2352), ("1:05:30", 3930), ("2100", 2100), ("", 0), ("kaputt", 0),
])
def test_duration_parser_directly(raw, expected):
    assert podcast.parse_duration(raw) == expected


def test_m4a_is_accepted(parsed):
    """One of the eight shows ships m4a. Assuming mp3 would drop it."""
    assert parsed["episodes"][0]["audio"].endswith(".m4a")
    assert "m4a" in parsed["episodes"][0]["audio_type"]


def test_published_transcript_is_found(parsed):
    """The whole prize: about a quarter of shows publish one, and it beats
    anything Whisper produces."""
    t = parsed["episodes"][0]["transcripts"]
    assert len(t) == 1 and t[0]["type"] == "vtt"


def test_episode_without_transcript_has_none(parsed):
    assert parsed["episodes"][1]["transcripts"] == []


def test_shownotes_keep_their_links(parsed):
    """Often the most valuable part for research — many shows put their
    sources there. Stripping to plain text would throw them away."""
    assert "[Quelle](https://example.com/q)" in parsed["episodes"][0]["shownotes"]


def test_broken_feed_does_not_raise():
    assert podcast.parse_feed(b"not xml at all") == {}
    assert podcast.parse_feed(b"<rss></rss>") == {}


# ── Spotify ───────────────────────────────────────────────────────────────────
#
# These exist because 532 green tests did not notice that from_spotify had
# stopped working: everything downstream was tested against fixtures, and the
# one function that touches Spotify was tested against nothing. So each test
# here feeds the function a *real* response shape, including the broken one.

EP = "https://open.spotify.com/episode/0000000000000000000000?si=xyz"

# What open.spotify.com actually answers a non-browser client as of 10/2026:
# 930 bytes of JS shell, no og: tag anywhere in it.
SHELL = (b'<!DOCTYPE html><html><head><meta charSet="utf-8"/>'
         b'<title>Spotify \xe2\x80\x93 Web Player</title></head><body></body></html>')

EMBED = ('<script id="__NEXT_DATA__" type="application/json">' + json.dumps({
    "props": {"pageProps": {"state": {"data": {"entity": {
        "name": "#64 Was passiert, wenn KI-Agenten gehackt werden?",
        "subtitle": "Beispiel-Podcast",
        "duration": 1163760,
        "releaseDate": {"isoString": "2026-10-05T02:49:00Z"},
    }}}}}}) + '</script>').encode()


def _fetch_map(mapping):
    """Answer by substring of the requested URL; None for anything else."""
    async def fake(url, timeout=20, as_json=False):
        for needle, answer in mapping.items():
            if needle in url:
                return answer
        return None
    return fake


def test_spotify_reads_the_embed_page(monkeypatch):
    """The embed page carries the show name as a field. No prose to split."""
    monkeypatch.setattr(podcast, "fetch", _fetch_map({"/embed/episode/": EMBED}))
    meta = asyncio.run(podcast.from_spotify(EP))
    assert meta["show"] == "Beispiel-Podcast"
    assert meta["date"] == "2026-10-05"
    # Spotify counts milliseconds, the matcher seconds. Mixing them up puts
    # every episode 1000x outside the five-second tolerance.
    assert meta["duration_s"] == 1163


def test_spotify_shell_page_alone_is_not_an_answer(monkeypatch):
    """The regression. The shell has a <title>, so anything that reads the
    page title instead of og:title would call this a success and return the
    string 'Spotify – Web Player' as the episode name."""
    monkeypatch.setattr(podcast, "fetch", _fetch_map({"/episode/": SHELL}))
    assert asyncio.run(podcast.from_spotify(EP)) == {}


def test_spotify_falls_back_to_og_tags(monkeypatch):
    """Kept because Spotify has reverted this kind of change before."""
    page = (b'<meta property="og:title" content="Daten im Betrieb"/>'
            b'<meta property="og:description" content="Beispiel-Podcast \xc2\xb7 Folge"/>'
            b'<meta property="music:duration" content="1163"/>')
    monkeypatch.setattr(podcast, "fetch", _fetch_map({"/episode/": page}))
    meta = asyncio.run(podcast.from_spotify(EP))
    assert meta["title"] == "Daten im Betrieb" and meta["show"] == "Beispiel-Podcast"


def test_spotify_oembed_yields_a_title_but_says_so(monkeypatch):
    """A title without a show cannot find a feed. It can still tell the user
    the episode exists, which is a different message than 'not readable'."""
    monkeypatch.setattr(podcast, "fetch", _fetch_map({
        "/episode/": SHELL, "/oembed": {"title": "#64 KI-Agenten"}}))
    meta = asyncio.run(podcast.from_spotify(EP))
    assert meta["title"] == "#64 KI-Agenten" and not meta["show"]
    assert "Titel" in meta["note"]


# ── Matching ──────────────────────────────────────────────────────────────────

def test_guid_wins(parsed):
    ep, how, _ = podcast.match_episode({"guid": "abc-123"}, parsed["episodes"])
    assert how == "guid" and ep["guid"] == "abc-123"


def test_duration_and_date_identify_without_a_guid(parsed):
    """A Spotify link carries neither feed nor GUID — only a title and a
    duration. This is the path that actually runs most of the time."""
    ep, how, _ = podcast.match_episode(
        {"duration_s": 2352, "date": "Thu, 18 Sep 2026 06:00:00 +0000"},
        parsed["episodes"])
    assert how == "dauer+datum" and ep["guid"] == "abc-123"


def test_duration_tolerance_is_narrow(parsed):
    """Five seconds. Wider and two episodes of a weekly show start colliding."""
    ep, how, _ = podcast.match_episode(
        {"duration_s": 2352 + 4, "date": "Thu, 18 Sep 2026 06:00:00 +0000"},
        parsed["episodes"])
    assert how == "dauer+datum"
    _, how, _ = podcast.match_episode(
        {"duration_s": 2352 + 60, "date": "Thu, 18 Sep 2026 06:00:00 +0000"},
        parsed["episodes"])
    assert how != "dauer+datum"


def test_feed_title_suffix_does_not_defeat_matching():
    """The concrete reason title matching is third: feeds carry guest names
    after a pipe that Spotify strips. Without normalising, a correct match
    scores badly."""
    assert podcast.title_similarity(
        "Daten im Betrieb",
        "Folge 12: Daten im Betrieb | Dr. Anna Beispiel, CTO") > 0.9


def test_episode_numbering_is_stripped():
    assert podcast.normalise_title("Folge 12: Daten") == podcast.normalise_title("Daten")


def test_weak_title_match_is_flagged_not_guessed(parsed):
    """Never silently raten — the concept says so twice, and a wrong episode
    quoted in a client conversation is the reason."""
    ep, how, note = podcast.match_episode({"title": "Völlig anderes Thema"},
                                          parsed["episodes"])
    assert how == "unsicher" and note and ep is not None


def test_two_similar_titles_are_flagged(parsed):
    ep, how, note = podcast.match_episode({"title": "Folge 1"}, parsed["episodes"])
    assert how == "unsicher"


def test_empty_feed_is_reported(parsed):
    ep, how, note = podcast.match_episode({"guid": "x"}, [])
    assert ep is None and note


# ── Order sheet ───────────────────────────────────────────────────────────────

def test_plain_links_are_accepted():
    """What a person actually pastes."""
    items = podcast.parse_order_sheet(
        "https://open.spotify.com/episode/abc\n\nhttps://example.com/feed.xml")
    assert len(items) == 2


def test_comments_and_blank_lines_are_skipped():
    assert podcast.parse_order_sheet("# Notiz\n\nhttps://a.de/x") == ["https://a.de/x"]


def test_order_sheet_json_is_accepted():
    """What an assistant produces when it has already resolved the episodes."""
    sheet = json.dumps({"schema": podcast.SCHEMA,
                        "episodes": [{"source": "https://x", "guid": "g1"}]})
    items = podcast.parse_order_sheet(sheet)
    assert items and items[0]["guid"] == "g1"


def test_malformed_json_falls_back_to_link_scanning():
    """A truncated paste should still do something useful."""
    assert podcast.parse_order_sheet('{"episodes": [ https://a.de/x') == ["https://a.de/x"]


def test_empty_input_yields_nothing():
    assert podcast.parse_order_sheet("") == []
    assert podcast.parse_order_sheet("   \n  ") == []


# ── Naming and notes ──────────────────────────────────────────────────────────

def test_slug_follows_the_agreed_shape():
    """<Show>_<date>_<title>, fixed in the concept so packages from different
    runs sort together."""
    slug = podcast.episode_slug({
        "show": "Werkstattfunk", "title": "Daten im Betrieb",
        "date": "Thu, 18 Sep 2026 06:00:00 +0000"})
    assert slug.startswith("Werkstattfunk_2026-09-18_")


def test_slug_survives_a_missing_date():
    assert "ohne-datum" in podcast.episode_slug({"show": "X", "title": "Y"})


def test_slug_has_no_path_separators():
    """It becomes a directory name."""
    slug = podcast.episode_slug({"show": "A/B", "title": "C:D\\E", "date": ""})
    assert "/" not in slug and "\\" not in slug and ":" not in slug


def test_shownotes_front_matter_is_machine_readable():
    md = podcast.shownotes_markdown({
        "title": "Titel", "show": "Sendung", "author": "Wer",
        "date": "Thu, 18 Sep 2026 06:00:00 +0000", "duration_s": 2352,
        "feed": "https://f", "guid": "g", "audio": "https://a.mp3",
        "shownotes": "Text", "transcripts": [{"url": "https://t.vtt", "type": "vtt"}]})
    assert md.startswith("---")
    assert "sendung: Sendung" in md
    assert "transkript_quelle: offiziell" in md
    assert "https://t.vtt" in md


def test_shownotes_marks_whisper_when_no_transcript():
    md = podcast.shownotes_markdown({"title": "T", "show": "S", "transcripts": []})
    assert "transkript_quelle: whisper" in md


def test_manifest_carries_the_schema():
    m = podcast.build_manifest([{"status": "ok", "title": "T", "guid": "g"}])
    assert m["schema"] == podcast.SCHEMA and m["episodes"][0]["guid"] == "g"


# ── Audio handling ────────────────────────────────────────────────────────────

def test_artwork_stream_is_dropped():
    """-vn is not optional. Several hosters embed episode artwork as a second
    stream; without it the file comes out twice the size — 30 MB instead of 14
    for a 39-minute episode."""
    src = inspect.getsource(main._shrink_audio)
    assert '"-vn"' in src


def test_long_episodes_get_a_lower_bitrate():
    src = inspect.getsource(main._shrink_audio)
    assert "PODCAST_LONG_MINUTES" in src and "PODCAST_LONG_KBPS" in src


def test_audio_is_mono():
    """Speech. Stereo doubles the size for nothing."""
    assert '"-ac", "1"' in inspect.getsource(main._shrink_audio)


def test_encoding_is_thread_limited():
    """This box also carries a wiki, a git forge and client sites."""
    assert '"-threads", "2"' in inspect.getsource(main._shrink_audio)


def test_downloads_resume_and_retry():
    """Podcast hosters are not nine-nines CDNs, and a truncated file that
    looks complete is worse than a failure: Whisper would transcribe twenty
    minutes and report success."""
    src = inspect.getsource(main._fetch_to_file)
    assert "--retry" in src and '"-C", "-"' in src


def test_tiny_downloads_are_rejected():
    """An error page saved as .mp3 is still a file."""
    assert "1024" in inspect.getsource(main._fetch_to_file)


# ── Whisper script ────────────────────────────────────────────────────────────

def _script(eps, kind="mac"):
    return main.build_podcast_whisper_script(eps, kind)


def test_no_script_when_every_episode_has_a_transcript():
    assert _script([{"status": "ok", "transcripts": [{"url": "x", "type": "vtt"}]}]) == ""


def test_script_covers_only_episodes_that_need_it():
    eps = [{"status": "ok", "show": "A", "title": "Mit", "date": "",
            "transcripts": [{"url": "x", "type": "vtt"}]},
           {"status": "ok", "show": "B", "title": "Ohne", "date": "", "transcripts": []}]
    s = _script(eps)
    assert "Ohne" in s and "Mit" not in s


def test_script_has_no_set_e():
    """One failing episode must not abandon the rest — the same lesson the
    download script learned the expensive way.

    Checks executable lines only: the comment that explains the absence
    legitimately contains the string, and a plain search cannot tell the
    explanation from the thing it warns against.
    """
    s = _script([{"status": "ok", "show": "S", "title": "T", "transcripts": []}])
    code = [l for l in s.splitlines() if not l.lstrip().startswith("#")]
    assert not any(l.strip().startswith("set -e") for l in code)


def test_script_avoids_array_indices():
    """zsh counts from one. This has bitten this project before."""
    s = _script([{"status": "ok", "show": "S", "title": "T", "transcripts": []}])
    assert "${0:a:h}" in s          # script directory, not an index


def test_script_splits_long_episodes():
    """A 103-minute episode died twice mid-run on a machine with 8 GB."""
    s = _script([{"status": "ok", "show": "S", "title": "T", "transcripts": []}])
    assert "SPLIT_AFTER=2700" in s and "segment_time 1200" in s


def test_script_picks_the_engine_by_machine():
    s = _script([{"status": "ok", "show": "S", "title": "T", "transcripts": []}])
    assert "arm64" in s and "mlx_whisper" in s and "whisper-ctranslate2" in s


def test_script_says_what_it_chose():
    """'The script does not ask, but shows what it chose.'"""
    s = _script([{"status": "ok", "show": "S", "title": "T", "transcripts": []}])
    assert "Engine:" in s


def test_script_seeds_proper_nouns():
    """Measured: an uncommon surname came back spelled two different ways."""
    s = _script([{"status": "ok", "show": "Werkstattfunk", "title": "T",
                  "author": "Erika Mustermann", "transcripts": []}])
    assert "initial_prompt" in s or "initial-prompt" in s
    assert "Werkstattfunk" in s


def test_prompt_is_capped():
    """Past roughly 200 characters Whisper starts echoing the prompt into the
    transcript."""
    ep = {"show": "S" * 300, "author": "A" * 300, "shownotes": "", "hints": "H" * 300}
    assert len(main._whisper_prompt(ep)) <= 200


def test_prompt_picks_names_from_the_notes():
    ep = {"show": "Sendung", "author": "", "shownotes": "Zu Gast ist Anna Beispiel heute."}
    assert "Anna Beispiel" in main._whisper_prompt(ep)


def test_windows_script_continues_past_errors():
    s = _script([{"status": "ok", "show": "S", "title": "T", "transcripts": []}], "windows")
    assert "ErrorActionPreference = 'Continue'" in s


def test_windows_uses_a_modest_model():
    """Without a GPU anything larger stops being a wait and becomes an
    overnight job."""
    s = _script([{"status": "ok", "show": "S", "title": "T", "transcripts": []}], "windows")
    assert "small" in s


def test_script_builds_timestamped_text():
    """The form you quote from — [mm:ss] per line."""
    s = _script([{"status": "ok", "show": "S", "title": "T", "transcripts": []}])
    assert "zeitmarken" in s


# ── Endpoints ─────────────────────────────────────────────────────────────────

def test_routes_exist():
    paths = {r.path for r in main.app.routes}
    assert "/api/podcast" in paths
    assert "/api/podcast/{pid}" in paths
    assert "/api/podcast/{pid}/zip" in paths


@pytest.mark.parametrize("fn", [main.podcast_status, main.podcast_zip, main.podcast_script])
def test_endpoints_check_ownership(fn):
    assert "_own_podcast" in inspect.getsource(fn)


def test_batch_size_is_capped():
    src = inspect.getsource(main.podcast_start)
    assert "PODCAST_MAX_EPISODES" in src


def test_audio_option_is_validated():
    src = inspect.getsource(main.podcast_start)
    assert '("klein", "original", "nein")' in src


def test_resolution_is_concurrency_bounded():
    """Apple starts refusing at roughly twenty calls a minute."""
    assert "Semaphore" in inspect.getsource(podcast.resolve_many)


def test_one_failure_does_not_sink_the_batch():
    src = inspect.getsource(podcast.resolve_many)
    assert "except Exception" in src


def test_feed_cache_is_show_level_only():
    """Caching show → feed for a day is not user data and does not contradict
    the no-history rule. Caching episodes would."""
    src = inspect.getsource(podcast)
    assert "FEED_CACHE_TTL" in src
    assert "_feed_cache[key] = (time.time(), best)" in src


def test_server_never_transcribes_podcasts():
    """Whisper runs on the person's machine. This VPS carries a wiki, a git
    forge and client sites, and has no swap."""
    src = inspect.getsource(main._podcast_job)
    assert "_whisper_segments" not in src and "mlx_whisper" not in src


# ── Der Auftragszettel-Brief ──────────────────────────────────────────────────
#
# Eine Anleitung, die niemand liest außer einem Assistenten, veraltet lautlos:
# der Assistent produziert dann brav die alte Form, und der Fehler landet beim
# Nutzer als "Folge nicht gefunden". Diese Tests halten Brief und Parser
# zusammen.

def test_brief_only_names_fields_the_code_reads():
    """Der eigentliche Zweck. Jedes Feld im Brief muss irgendwo im Auflöser
    oder im Abgleich auch gelesen werden — sonst verspricht die Anleitung
    etwas, das keine Wirkung hat."""
    # NICHT das ganze Modul durchsuchen: dort steht ORDER_SHEET_FIELDS selbst
    # drin, also kommt jeder Feldname als Literal vor und die Zusicherung ist
    # für jedes erfundene Feld grün. Genau so war dieser Test zuerst
    # geschrieben — ein Test, der nicht fehlschlagen kann, ist schlimmer als
    # keiner, weil er Sicherheit vortäuscht.
    src = "".join(inspect.getsource(f) for f in (
        podcast.resolve, podcast.match_episode, podcast.parse_order_sheet,
        podcast.build_manifest, podcast.from_spotify))
    for field in podcast.ORDER_SHEET_FIELDS:
        assert (f'"{field}"' in src or f"'{field}'" in src), \
            f"Brief nennt »{field}«, der Code liest es nirgends"


def test_brief_carries_the_schema_the_parser_accepts():
    brief = podcast.order_sheet_brief()
    assert podcast.SCHEMA in brief
    # Das Beispiel im Brief muss selbst durch den Parser gehen. Ein Beispiel,
    # das der eigene Parser ablehnt, ist schlimmer als keins.
    block = brief.split("```json", 1)[1].split("```", 1)[0]
    items = podcast.parse_order_sheet(block)
    assert len(items) == 1 and items[0]["guid"]
    assert isinstance(items[0]["duration_s"], int)


def test_brief_warns_about_milliseconds():
    """Die Falle, die in der Praxis zuschlägt: Spotify liefert Millisekunden,
    der Abgleich erwartet Sekunden mit fünf Sekunden Toleranz."""
    brief = podcast.order_sheet_brief()
    assert "Millisekunden" in brief and "Sekunden" in brief


def test_brief_names_its_own_address_when_asked():
    assert "https://fundus.example" in podcast.order_sheet_brief("https://fundus.example/")


def test_brief_is_reachable_without_login(client):
    """Der Sinn der Adresse ist, dass ein Assistent ohne Sitzung sie abrufen
    kann. Hinter dem Login wäre sie wertlos."""
    r = client.get("/api/podcast/auftragszettel.md")
    assert r.status_code == 200
    assert podcast.SCHEMA in r.text
    assert "markdown" in r.headers["content-type"]


# ── Datum mit und ohne Zeitzone ───────────────────────────────────────────────

def test_rfc822_without_offset_still_gets_a_timezone():
    """Der Absturz. `parsedate_to_datetime` gibt ein Datum OHNE Zeitzone
    zurück, wenn der Feed keine nennt — ein RFC-822-Datum darf den Offset
    weglassen, und `-0000` heißt ausdrücklich »unbekannt«. Der Zweig für
    ISO-Daten normalisierte sorgfältig auf UTC, dieser gab durch, was er
    bekam.

    Die Folge war kein falsches Datum, sondern ein Abbruch mitten im
    Abgleich: »can't subtract offset-naive and offset-aware datetimes« — eine
    Meldung, die wie ein Fehler im Auftragszettel aussieht und keiner war.
    """
    for raw in ("Mon, 05 Oct 2026 02:49:00",
                "Mon, 05 Oct 2026 02:49:00 -0000",
                "5 Oct 2026 02:49:00"):
        d = podcast.parse_date(raw)
        assert d is not None and d.tzinfo is not None, raw


def test_dates_from_any_source_can_be_subtracted():
    """Die Eigenschaft, auf die es ankommt. Der Abgleich rechnet
    `(feed_datum - gesuchtes_datum).days`, und dafür müssen beide aus
    derselben Welt kommen — egal, aus welchem Format sie stammen."""
    forms = ["Mon, 05 Oct 2026 02:49:00",          # ohne Offset
             "Mon, 05 Oct 2026 04:49:00 +0200",    # mit Offset
             "2026-10-05T02:49:00Z",               # ISO mit Z
             "2026-10-05"]                         # nur Datum
    dates = [podcast.parse_date(f) for f in forms]
    assert all(d is not None for d in dates)
    for a in dates:
        for b in dates:
            abs((a - b).days)          # darf nicht werfen


def test_offsetless_feed_date_still_matches(parsed):
    """Zusammen mit dem Abgleich: eine Folge, deren Feed kein Offset führt,
    muss weiter über Dauer und Datum gefunden werden. Vorher brach hier der
    ganze Auftrag ab, nicht nur diese Folge."""
    eps = [dict(e) for e in parsed["episodes"]]
    for e in eps:
        if e.get("date"):
            e["date"] = "Thu, 18 Sep 2026 06:00:00"     # Offset entfernt
    ep, how, _ = podcast.match_episode(
        {"duration_s": 2352, "date": "Thu, 18 Sep 2026 06:00:00 +0000"}, eps)
    assert how == "dauer+datum"


# ── Feeds von früher ──────────────────────────────────────────────────────────
#
# Fundus merkt sich Feeds absichtlich nicht selbst: das Produkt verspricht an
# drei Stellen „keine Historie", und eine Tabelle auf der Platte wäre ein
# Verzeichnis der Sendungen, die jemand aufgelöst hat. Das Wissen liegt
# stattdessen in den Dateien des Nutzers und reist mit dem Auftragszettel.

def test_manifest_can_be_thrown_back_in_unchanged():
    """Die eigentliche Bequemlichkeit: kein Vorbereiten. Das manifest.json
    eines früheren Laufs ist schon ein gültiger Auftragszettel, und die
    Feeds stehen bei jeder Folge drin."""
    manifest = json.dumps({
        "schema": podcast.SCHEMA,
        "episodes": [{"source": "https://open.spotify.com/episode/A",
                      "show": "Beispiel-Podcast", "feed": "https://example.com/r.xml"}],
    })
    assert podcast.order_sheet_feeds(manifest) == {
        "Beispiel-Podcast": "https://example.com/r.xml"}
    assert len(podcast.parse_order_sheet(manifest)) == 1


def test_fundus_writes_the_table_into_its_own_manifest():
    """Ausdrücklich, nicht nur nebenbei in den Folgen — sonst gehen Sendungen
    verloren, von denen man später keine Folge mehr im Zettel hat."""
    m = podcast.build_manifest([{"show": "Doppelgänger", "feed": "https://example.com/f",
                                 "status": "ok", "title": "t", "guid": "g"}])
    assert m["feeds"] == {"Doppelgänger": "https://example.com/f"}


def test_hint_matching_survives_different_spellings():
    """Der Sendungsname kommt aus drei Quellen und ist selten dreimal gleich
    geschrieben: Spotify schneidet Zusätze ab, Feeds führen sie, und wer von
    Hand tippt, lässt Satzzeichen weg."""
    feeds = {"Doppelgänger": "https://example.com/f"}
    for probe in ("Doppelgänger", "doppelgänger", "Folge 12: Doppelgänger"):
        assert podcast.feed_from_hints(feeds, probe) == "https://example.com/f", probe
    assert podcast.feed_from_hints(feeds, "Etwas anderes") == ""


def test_explicit_table_beats_one_derived_from_episodes():
    """Eine ausdrückliche Angabe ist die bewusstere; die aus den Folgen
    abgeleitete ist nur ein Nebenprodukt und darf sie nicht überschreiben."""
    sheet = json.dumps({
        "feeds": {"Show": "https://example.com/gewollt"},
        "episodes": [{"show": "Show", "feed": "https://example.com/nebenbei"}],
    })
    assert podcast.order_sheet_feeds(sheet)["Show"] == "https://example.com/gewollt"


def test_hint_feeds_go_through_the_url_guard():
    """Die Adresse kommt aus einer Datei, die der Nutzer irgendwo bekommen
    hat. Ein Feed auf 127.0.0.1 wäre genau der SSRF-Weg, den das
    Security-Review geschlossen hat — die Prüfung darf hier nicht fehlen."""
    assert podcast.feed_from_hints({"S": "http://127.0.0.1:8080/f"}, "S") == ""
    assert podcast.feed_from_hints({"S": "file:///etc/passwd"}, "S") == ""


def test_plain_link_list_yields_no_feeds():
    assert podcast.order_sheet_feeds("https://open.spotify.com/episode/A") == {}
    assert podcast.order_sheet_feeds("") == {}
