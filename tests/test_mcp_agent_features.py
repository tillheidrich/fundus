"""What an assistant needs to work with a podcast package on its own.

Before these tools, an assistant could start a package and search it, but not
see how far it was, read a single file without the whole ZIP, quote a hit with
a source, or page through a transcript longer than its context. Each test
below is one of those gaps, checked by calling the tool and reading the answer.
"""
import asyncio
import importlib.util
import json
import uuid
from pathlib import Path

import pytest

import db
import main
import mcp_tools
import podcast
from test_mcp_podcast import offline  # noqa: F401  (fixture)

EP_OK = {"status": "ok", "match": "guid", "show": "Beispielsendung",
         "title": "Haftung bei KI-Agenten", "date": "2026-10-05",
         "audio": "https://example.com/folge.mp3",
         "source": "https://open.spotify.com/episode/x"}
EP_UNSURE = {"status": "unsicher", "match": "titel", "note": "Nur über den Titel.",
             "show": "Andere Sendung", "title": "GPUs", "date": "2026-10-02",
             "source": "https://example.org/gpus"}
EP_MISSING = {"status": "nicht_aufgeloest", "note": "Feed nicht gefunden.",
              "source": "https://x/nope"}

VTT = ("WEBVTT\n\n00:00:05.000 --> 00:00:09.000\nHeute geht es um Haftung.\n\n"
       "00:23:14.000 --> 00:23:19.000\nDie Produkthaftung gilt ab Dezember auch "
       "für Software.\n")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def package(tmp_path, monkeypatch):
    """A finished package owned by user 1, laid out as _podcast_job does it."""
    pid = str(uuid.uuid4())
    root = tmp_path / f"podcast_{pid}"
    root.mkdir()
    ok, unsure = podcast.episode_slug(EP_OK), podcast.episode_slug(EP_UNSURE)
    (root / ok).mkdir()
    (root / ok / f"{ok}_Transkript.vtt").write_text(VTT, encoding="utf-8")
    (root / ok / f"{ok}_Transkript.txt").write_text(
        "Heute geht es um Haftung. " * 50, encoding="utf-8")
    (root / ok / f"{ok}_Shownotes.md").write_text("---\nsendung: X\n---\nNotizen",
                                                  encoding="utf-8")
    (root / unsure).mkdir()
    (root / unsure / f"{unsure}_Transkript.txt").write_text(
        "Über Haftung sprechen wir später.\n", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(podcast.build_manifest([EP_OK, EP_UNSURE, EP_MISSING])),
        encoding="utf-8")

    monkeypatch.setattr(main, "_podcast_dir", lambda p: tmp_path / f"podcast_{p}")
    main.podcasts[pid] = {"status": "done", "stage": "", "user_id": 1, "total": 3,
                          "done": 3, "episodes": [dict(EP_OK), dict(EP_UNSURE),
                                                  dict(EP_MISSING)]}
    mcp_tools.current_mcp_user.set({"id": 1})
    try:
        yield {"pid": pid, "root": root, "ok": ok, "unsure": unsure}
    finally:
        mcp_tools.current_mcp_user.set(None)
        main.podcasts.pop(pid, None)


# ── get_podcast_package_status ───────────────────────────────────────────────

def test_status_reports_progress_and_each_episode(package):
    r = run(mcp_tools.get_podcast_package_status(package["pid"]))
    assert (r["status"], r["done"], r["total"]) == ("done", 3, 3)
    by_title = {e["title"]: e for e in r["episodes"]}
    ok = by_title["Haftung bei KI-Agenten"]
    assert ok["show"] == "Beispielsendung" and ok["has_transcript"] is True
    assert ok["episode"] == package["ok"]
    assert by_title["GPUs"]["status_code"] == "uncertain"


def test_status_marks_unresolved_episodes_without_a_folder(package):
    r = run(mcp_tools.get_podcast_package_status(package["pid"]))
    missing = next(e for e in r["episodes"] if e["status"] == "nicht_aufgeloest")
    assert missing["status_code"] == "unresolved"
    assert missing["has_transcript"] is False and missing["episode"] == ""


def test_status_sees_a_missing_transcript_on_disk(package):
    """has_transcript comes from the files, so a failed download shows up."""
    for f in (package["root"] / package["unsure"]).glob("*_Transkript.*"):
        f.unlink()
    r = run(mcp_tools.get_podcast_package_status(package["pid"]))
    assert next(e for e in r["episodes"] if e["title"] == "GPUs")["has_transcript"] is False


def test_status_refuses_someone_elses_package(package):
    mcp_tools.current_mcp_user.set({"id": 2})
    assert "error" in run(mcp_tools.get_podcast_package_status(package["pid"]))


def test_status_needs_a_user(package):
    mcp_tools.current_mcp_user.set(None)
    assert "error" in run(mcp_tools.get_podcast_package_status(package["pid"]))


def test_status_unknown_id_looks_like_foreign_id(package):
    """Same answer either way, so the reply does not confirm a run exists."""
    unknown = run(mcp_tools.get_podcast_package_status(str(uuid.uuid4())))
    mcp_tools.current_mcp_user.set({"id": 2})
    foreign = run(mcp_tools.get_podcast_package_status(package["pid"]))
    assert unknown == foreign


# ── read_podcast_package_file ────────────────────────────────────────────────

def test_read_transcript_prefers_plain_text(package):
    r = run(mcp_tools.read_podcast_package_file(package["pid"], package["ok"]))
    assert r["file"].endswith("_Transkript.txt")
    assert r["text"].startswith("Heute geht es um Haftung.")
    assert r["next_offset"] is None


def test_read_pages_through_a_long_file(package):
    pid, ep = package["pid"], package["ok"]
    first = run(mcp_tools.read_podcast_package_file(pid, ep, max_chars=100))
    assert len(first["text"]) == 100 and first["next_offset"] == 100
    collected, offset = first["text"], first["next_offset"]
    while offset is not None:
        page = run(mcp_tools.read_podcast_package_file(pid, ep, offset=offset,
                                                       max_chars=300))
        collected += page["text"]
        offset = page["next_offset"]
    assert collected == (package["root"] / ep / f"{ep}_Transkript.txt").read_text()
    assert first["total_chars"] == len(collected)


def test_read_shownotes_and_manifest(package):
    notes = run(mcp_tools.read_podcast_package_file(package["pid"], package["ok"],
                                                    "shownotes"))
    assert "Notizen" in notes["text"]
    man = run(mcp_tools.read_podcast_package_file(package["pid"], kind="manifest"))
    assert json.loads(man["text"])["schema"] == podcast.SCHEMA


@pytest.mark.parametrize("episode", ["../../etc", "/etc", "..", "", "nicht-im-paket"])
def test_read_only_accepts_known_episodes(package, episode):
    r = run(mcp_tools.read_podcast_package_file(package["pid"], episode))
    assert "error" in r and "text" not in r
    # The error lists the valid names, so the caller can correct itself.
    assert package["ok"] in r["episodes"]


def test_read_does_not_follow_a_symlink_out_of_the_package(package, tmp_path):
    secret = tmp_path / "geheim.txt"
    secret.write_text("nicht lesen", encoding="utf-8")
    ep = package["unsure"]
    (package["root"] / ep / f"{ep}_Shownotes.md").symlink_to(secret)
    r = run(mcp_tools.read_podcast_package_file(package["pid"], ep, "shownotes"))
    assert "error" in r and "nicht lesen" not in json.dumps(r)


def test_read_rejects_unknown_kind(package):
    r = run(mcp_tools.read_podcast_package_file(package["pid"], package["ok"], "audio"))
    assert "error" in r


def test_read_refuses_someone_elses_package(package):
    mcp_tools.current_mcp_user.set({"id": 2})
    r = run(mcp_tools.read_podcast_package_file(package["pid"], package["ok"]))
    assert "error" in r and "text" not in r


# ── Citations ────────────────────────────────────────────────────────────────

def test_hit_carries_a_quotable_citation(package):
    hits = main.search_transcripts(package["root"], "Produkthaftung",
                                   episodes=[EP_OK, EP_UNSURE])
    assert len(hits) == 1
    assert hits[0]["cite"] == ("“Die Produkthaftung gilt ab Dezember auch für Software.” · "
                               "Beispielsendung, Haftung bei KI-Agenten (2026-10-05), 23:14")


def test_direct_audio_link_jumps_to_the_moment(package):
    hit = main.search_transcripts(package["root"], "Produkthaftung",
                                  episodes=[EP_OK])[0]
    assert hit["link"] == "https://example.com/folge.mp3#t=1394"


def test_untimed_hit_has_no_time_in_cite_or_link(package):
    """No invented time: plain text hits keep the page link, without #t."""
    hit = main.search_transcripts(package["root"], "später", episodes=[EP_UNSURE])[0]
    assert hit["link"] == "https://example.org/gpus"
    assert hit["cite"].endswith("GPUs (2026-10-02)")


def test_citation_metadata_falls_back_to_the_manifest(package):
    """Callers that only have the folder still get show and title."""
    hit = main.search_transcripts(package["root"], "Produkthaftung")[0]
    assert "Beispielsendung" in hit["cite"] and hit["link"].endswith("#t=1394")


def test_existing_hit_fields_are_kept(package):
    hit = main.search_transcripts(package["root"], "Produkthaftung")[0]
    assert {"episode", "at", "seconds", "text", "cite", "link"} <= set(hit)


def test_mcp_search_returns_citations(package):
    r = run(mcp_tools.search_podcast_transcripts(package["pid"], "Haftung"))
    assert r["count"] >= 2 and all(h["cite"].startswith("“") for h in r["hits"])


def test_http_search_returns_citations(package, client, alice, as_user):
    main.podcasts[package["pid"]]["user_id"] = alice["id"]
    r = as_user(alice).get(f"/api/podcast/{package['pid']}/search",
                           params={"q": "Produkthaftung"})
    assert r.status_code == 200
    assert r.json()["hits"][0]["link"] == "https://example.com/folge.mp3#t=1394"


# ── Pagination ───────────────────────────────────────────────────────────────

def test_podcast_transcript_unchanged_without_paging(offline):  # noqa: F811
    r = run(mcp_tools.get_podcast_transcript("https://x/a"))
    assert "next_offset" not in r and "Zweiter Satz" in r["text"]


def test_podcast_transcript_pages(offline):  # noqa: F811
    full = run(mcp_tools.get_podcast_transcript("https://x/a", "timestamps"))["text"]
    first = run(mcp_tools.get_podcast_transcript("https://x/a", "timestamps",
                                                 max_chars=10))
    assert first["text"] == full[:10] and first["next_offset"] == 10
    rest = run(mcp_tools.get_podcast_transcript("https://x/a", "timestamps",
                                                offset=10, max_chars=10_000))
    assert first["text"] + rest["text"] == full and rest["next_offset"] is None


@pytest.fixture
def threads_post(monkeypatch):
    """get_transcript's Threads branch with the network taken out."""
    body = "".join(f"Satz {i}. " for i in range(200))

    async def fake_download(jid, url, cookies, _):
        main.jobs[jid].update(transcript=body, meta={"title": "Post"})
    monkeypatch.setattr(main, "_is_threads", lambda u: True)
    monkeypatch.setattr(main, "_download_threads", fake_download)
    monkeypatch.setattr(db, "log_event", lambda *a: None)
    return body


def test_get_transcript_pages(threads_post):
    r = run(mcp_tools.get_transcript("https://threads.net/x", max_chars=50))
    assert r["text"] == threads_post[:50] and r["next_offset"] == 50
    assert r["total_chars"] == len(threads_post)
    whole = run(mcp_tools.get_transcript("https://threads.net/x"))
    assert whole["text"] == threads_post and "next_offset" not in whole


# ── Language-neutral status ──────────────────────────────────────────────────

def test_resolve_adds_status_codes_and_keeps_german(monkeypatch):
    async def many(items, concurrency=4):
        return [dict(EP_OK), dict(EP_UNSURE), dict(EP_MISSING)]
    monkeypatch.setattr(podcast, "resolve_many", many)
    r = run(mcp_tools.resolve_podcast(["a", "b", "c"]))
    assert [e["status"] for e in r["episodes"]] == ["ok", "unsicher", "nicht_aufgeloest"]
    assert [e["status_code"] for e in r["episodes"]] == ["ok", "uncertain", "unresolved"]
    assert r["episodes"][1]["note"] == "Nur über den Titel."
    assert r["summary"]["unsicher"] == 1
    assert r["summary"]["by_code"] == {"ok": 1, "uncertain": 1, "unresolved": 1}
    # The manifest is fed back in later; it stays in its own vocabulary.
    assert all("status_code" not in e for e in r["manifest"]["episodes"])


def test_shownotes_and_transcript_report_unresolved(monkeypatch):
    async def not_found(item):
        return dict(EP_MISSING)
    monkeypatch.setattr(podcast, "resolve", not_found)
    for r in (run(mcp_tools.get_podcast_shownotes("https://x/nope")),
              run(mcp_tools.get_podcast_transcript("https://x/nope"))):
        assert r["status_code"] == "unresolved" and r["note"] == "Feed nicht gefunden."


def test_search_hint_has_a_neutral_twin(package):
    r = run(mcp_tools.search_podcast_transcripts(package["pid"], "Haftung"))
    assert r["hinweis"] and r["note"] and r["status_code"] == "uncertain"
    timed = run(mcp_tools.search_podcast_transcripts(package["pid"], "Produkthaftung"))
    assert timed["status_code"] == "ok" and timed["note"] == ""


# ── Media tools are opt-in ───────────────────────────────────────────────────

def _fresh_tools(monkeypatch, value):
    """A separate copy of mcp_tools imported under the given environment.

    A copy, not a reload: main has already mounted the real module's app, and
    replacing it mid-run would leave the other tests talking to a stale one.
    """
    if value is None:
        monkeypatch.delenv("ENABLE_MEDIA_TOOLS", raising=False)
    else:
        monkeypatch.setenv("ENABLE_MEDIA_TOOLS", value)
    spec = importlib.util.spec_from_file_location(
        f"mcp_tools_probe_{uuid.uuid4().hex}", mcp_tools.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.registered_tool_names()


MEDIA = {"get_download_script", "get_livestream_script"}


@pytest.mark.parametrize("value", [None, "0", "", "nein"])
def test_media_tools_are_off_by_default(monkeypatch, value):
    names = _fresh_tools(monkeypatch, value)
    assert not (MEDIA & names)
    assert {"get_transcript", "get_podcast_package_status",
            "read_podcast_package_file"} <= names


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_media_tools_can_be_switched_on(monkeypatch, value):
    assert MEDIA <= _fresh_tools(monkeypatch, value)


def test_tool_list_in_the_interface_names_every_default_tool(monkeypatch):
    """The settings page is where people learn what the assistant can do."""
    html = (Path(main.__file__).parent / "templates" / "index.html").read_text(
        encoding="utf-8")
    line = next(l for l in html.splitlines() if "get_transcript · list_transcript_languages" in l)
    # The media tools are listed only when switched on (Jinja block).
    import re
    line = re.sub(r"\{% if media_tools %\}.*?\{% endif %\}", "", line)
    for name in _fresh_tools(monkeypatch, None):
        assert name in line
    for name in MEDIA:
        assert name not in line
