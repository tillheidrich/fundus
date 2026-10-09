"""FastMCP server mounted at /mcp. Auth is per-user: the Bearer token is the
user's api_token (checked in main's middleware, which sets current_mcp_user).

Tools are text-first (what an LLM can actually use): transcripts, captions,
metadata packages and ready-to-run local scripts. No server-side history."""
import os
import uuid
from contextvars import ContextVar
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import db

# Set by main.py's middleware after validating the Bearer token.
current_mcp_user: ContextVar[dict | None] = ContextVar("current_mcp_user", default=None)

# Instructions are what clients show the model before any tool call (Codex
# reads the first 512 characters), so they are short and in English: small
# open models pick tools noticeably better from English descriptions.
INSTRUCTIONS = (
    "Fundus turns podcasts and videos into text. Use get_transcript for a video or "
    "post URL. For podcasts: resolve_podcast, then get_podcast_package, poll "
    "get_podcast_package_status, then read_podcast_package_file or "
    "search_podcast_transcripts. Long texts are paged with offset/max_chars. "
    "Results may contain German field names; status_code is always English.")

mcp = FastMCP("fundus", instructions=INSTRUCTIONS, stateless_http=True,
              json_response=True, streamable_http_path="/")

# The two script builders hand an assistant a ready-made media download. That
# is a different legal position from transcripts and metadata, so an operator
# has to opt in. Read once at import: the tool list an MCP client sees is
# fixed at connect time anyway, and flipping it at runtime would only produce
# clients holding tools that no longer exist.
MEDIA_TOOLS_ENABLED = os.environ.get(
    "ENABLE_MEDIA_TOOLS", "0").strip().lower() in ("1", "true", "yes")

# The Mac app (same condition as main.DESKTOP_MODE, computed here because
# main imports this module). There the switch is a runtime setting in the
# System view, so the tools are always registered — a client's tool list is
# fixed at connect time — and refuse with an error while switched off.
_DESKTOP = (
    os.environ.get("LOCAL_MODE", "").strip().lower() in ("1", "true", "yes")
    and os.environ.get("FUNDUS_DESKTOP", "").strip() == "1"
    and not os.environ.get("TRUSTED_PROXIES", "").strip()
)


def media_tools_enabled() -> bool:
    """Server: the operator's ENABLE_MEDIA_TOOLS. Desktop: the db setting
    "media_tools" (off unless switched on with consent)."""
    if _DESKTOP:
        return db.get_setting("media_tools", "1" if MEDIA_TOOLS_ENABLED else "0") == "1"
    return MEDIA_TOOLS_ENABLED


_MEDIA_OFF = {
    "ok": False, "status_code": "disabled",
    "error": "Download tools are switched off. The user can enable them in "
             "Fundus under System → Media.",
}


def registered_tool_names() -> set[str]:
    """What an MCP client will actually be offered — the honest answer to
    "is this tool switched on", independent of how registration happened."""
    return {t.name for t in mcp._tool_manager.list_tools()}


def _uid() -> int | None:
    u = current_mcp_user.get()
    return u["id"] if u else None


def _bad_url(url) -> dict | None:
    """The error to hand back when a URL may not reach yt-dlp, else None.

    The web routes have run require_http_url for a while; the MCP tools took
    the URL straight from the assistant. yt-dlp reads anything starting with
    "-" as an option, so `--config-locations=/…` or `--exec …` from a prompt
    injection in some web page would have become a command line on this
    server. Same check as the web routes, same wording.
    """
    import main
    try:
        main.require_http_url(url if isinstance(url, str) else "")
    except main.HTTPException as e:
        return {"ok": False, "error": e.detail}
    return None


def _throttled(n: int) -> dict | None:
    """The hourly budget the web routes use, for the same expensive work.

    An assistant in a loop is the likeliest way to issue hundreds of these;
    the per-user batch budget is what keeps that from occupying the server.
    The desktop app raises the limit via LOCAL_MODE defaults.
    """
    import main
    uid = _uid()
    if not uid:
        return None
    try:
        main._batch_throttle(uid, n)
    except main.HTTPException as e:
        return {"ok": False, "error": e.detail}
    return None


# Episode status words are German because the package, the manifest and the
# UI are. An assistant working in another language should not have to guess
# that "unsicher" means "check before quoting", so every German status also
# travels as a fixed English code. The German keys stay: existing callers and
# saved manifests read them.
_STATUS_CODES = {"ok": "ok", "unsicher": "uncertain", "nicht_aufgeloest": "unresolved"}


def _status_code(status: str | None) -> str:
    # Anything unknown is reported as unresolved: claiming "ok" for a status
    # we cannot interpret would be the one wrong answer that gets quoted.
    return _STATUS_CODES.get(status or "", "unresolved")


def _page(text: str, offset: int = 0, max_chars: int = 0) -> tuple[str, int | None]:
    """Slice long text for callers with a small context window.

    max_chars=0 means "all of it", so a caller that never asks for paging gets
    exactly what it got before. next_offset is None once the end is reached —
    a number there means "call again", nothing else.
    """
    text = text or ""
    start = max(0, int(offset or 0))
    if max_chars and int(max_chars) > 0:
        end = start + int(max_chars)
        return text[start:end], (end if end < len(text) else None)
    return text[start:], None


def _apply_page(out: dict, key: str, offset: int, max_chars: int) -> dict:
    if key not in out or not isinstance(out[key], str):
        return out
    if not offset and not max_chars:
        return out                      # unchanged shape for old callers
    total = len(out[key])
    out[key], out["next_offset"] = _page(out[key], offset, max_chars)
    out["offset"] = max(0, int(offset or 0))
    out["total_chars"] = total
    return out


@mcp.tool(description="Get the transcript of a YouTube, Instagram or TikTok video, or the full text of a Threads post including its thread. lang='native' uses the video's original language, otherwise e.g. de, en, fr, es. format: 'text', 'timestamps' ([mm:ss] per line), 'segments' (start/dur/text), 'srt' or 'vtt'. For long texts set max_chars>0: returns up to that many characters from offset plus next_offset for the next call (null = end).")
async def get_transcript(url: str, lang: str = "native", format: str = "text",
                         offset: int = 0, max_chars: int = 0) -> dict:
    import main
    if (err := _bad_url(url)) or (err := _throttled(1)):
        return err
    url = url.strip()
    cookies = db.get_ig_cookies(_uid()) if _uid() else ""
    jid = f"mcp_{uuid.uuid4().hex}"

    # Threads: the post text is the content — no subtitles involved.
    if main._is_threads(url):
        main.jobs[jid] = {"status": "pending"}
        await main._download_threads(jid, url, cookies, False)
        job = main.jobs.pop(jid, {})
        for p in job.get("filepaths", []):
            main.Path(p).unlink(missing_ok=True)
        body = job.get("transcript", "")
        m = job.get("meta") or {}
        if not body:
            return {"ok": False, "error": job.get("error", "Kein Text gefunden.")}
        db.log_event(_uid(), "transcript")
        return _apply_page(
            {"ok": True, "platform": "Threads", "title": m.get("title"),
             "uploader": m.get("uploader"), "chain_len": m.get("chain_len", 1),
             "word_count": m.get("word_count"), "text": body},
            "text", offset, max_chars)

    # Same pipeline as the web UI, so both answer alike: YouTube captions via
    # the timedtext API (no extractor needed), yt-dlp subtitles elsewhere,
    # then local Whisper where it is allowed. Until 10/2026 this tool had a
    # path of its own that always needed yt-dlp (so it failed on the stock
    # image) and could pick a machine translation over the original track.
    main.jobs[jid] = {"status": "pending", "url": url, "type": "transcript",
                      "user_id": _uid()}
    try:
        await main._transcript_job(jid, url, lang, _uid())
    finally:
        job = main.jobs.pop(jid, None) or {}
    for f in main.TMP_DIR.glob(f"{jid}*"):
        f.unlink(missing_ok=True)

    meta = job.get("meta") or {}
    if job.get("status") == "done":
        segments = job.get("segments") or []
        db.log_event(_uid(), "transcript")
        out = {"ok": True, "title": meta.get("title"), "uploader": meta.get("uploader"),
               "url": meta.get("webpage_url") or url,
               "lang": job.get("used_lang") or "",
               "source": ("whisper" if meta.get("via_whisper") else "captions"),
               "word_count": job.get("word_count", 0),
               "duration": job.get("duration", 0)}
        if format == "segments":
            out["segments"] = segments
        elif format == "timestamps":
            out["transcript"] = main.segments_to_timestamped(segments)
        elif format == "srt":
            out["transcript"] = main.segments_to_srt(segments)
        elif format == "vtt":
            out["transcript"] = main.segments_to_vtt(segments)
        else:
            out["transcript"] = job.get("transcript", "")
        # word_count and duration describe the whole transcript, not the
        # page, so they are computed before slicing.
        return _apply_page(out, "transcript", offset, max_chars)

    caption = job.get("caption") or ""
    return {"ok": bool(caption), "title": meta.get("title"), "caption": caption,
            "blocked": bool(job.get("blocked")),
            "note": job.get("error") or "No transcript available."}


@mcp.tool(description="List the caption languages available for a YouTube video (code and whether auto-generated).")
async def list_transcript_languages(url: str) -> dict:
    import main
    if err := _bad_url(url):
        return err
    url = url.strip()
    # YouTube first through the same caption API the transcripts use: it
    # needs no extractor, so this works on the stock image too.
    vid = main._yt_video_id(url) if main._is_youtube(url) else ""
    if vid:
        def _list():
            from youtube_transcript_api import YouTubeTranscriptApi
            return [{"code": t.language_code, "name": t.language, "auto": bool(t.is_generated)}
                    for t in YouTubeTranscriptApi().list(vid)]
        try:
            langs = await main.asyncio.to_thread(_list)
            return {"count": len(langs), "languages": langs[:200]}
        except Exception as e:
            if not main._extractor_installed():
                return {"count": 0, "languages": [], "error": str(e)[:300]}
    if not main._extractor_installed():
        return {"count": 0, "languages": [], "error": main.YTDLP_MISSING}
    cookies = db.get_ig_cookies(_uid()) if _uid() else ""
    ck = None
    args = ["--list-subs", "--skip-download", "--no-playlist",
            "--extractor-args", main.YT_PLAYER_CLIENTS]
    if cookies.strip():
        ck = main.TMP_DIR / f"ls_{uuid.uuid4().hex}.txt"
        ck.write_text(cookies.strip())
        args = ["--cookies", str(ck)] + args
    _, out, _ = await main.run_ytdlp(args + ["--", url])
    if ck and ck.exists():
        ck.unlink()
    langs, auto = [], False
    for line in (out or "").splitlines():
        if "automatic captions" in line.lower():
            auto = True; continue
        if "available subtitles" in line.lower():
            auto = False; continue
        # The format column is a comma list ("vtt, srt, json3"); requiring a
        # single token there matched no real yt-dlp line at all.
        m = main.re.match(r"^([a-zA-Z][\w-]{1,10})\s+(\S.*?)\s{2,}(\S.*)$", line)
        if m and m.group(1) not in ("Language",):
            langs.append({"code": m.group(1), "name": m.group(2).strip(), "auto": auto})
    return {"count": len(langs), "languages": langs[:200]}


@mcp.tool(description="Get only the written caption or description text of a post, without media.")
async def get_caption(url: str) -> dict:
    import main
    if err := _bad_url(url):
        return err
    url = url.strip()
    if not main._extractor_installed():
        return {"ok": False, "error": main.YTDLP_MISSING}
    meta = await main._fetch_light_meta(url)
    caption = await main._fetch_caption(url)
    return {"title": meta.get("title"), "uploader": meta.get("uploader"), "caption": caption}


@mcp.tool(description="Build AI-ready Markdown metadata for several URLs: title, link, creator, date, description.")
async def get_metadata(urls: list[str]) -> dict:
    import main
    urls = list(urls or [])
    # Two yt-dlp runs per URL, sequentially: an unbounded list is an
    # unbounded amount of server time from one tool call.
    if len(urls) > main.BATCH_MAX_URLS:
        return {"ok": False,
                "error": f"{len(urls)} URLs — höchstens {main.BATCH_MAX_URLS} auf einmal."}
    for u in urls:
        if err := _bad_url(u):
            return {**err, "url": u}
    if err := _throttled(len(urls)):
        return err
    urls = [u.strip() for u in urls]
    out = ["# Video-Metadaten\n"]
    for i, url in enumerate(urls, 1):
        m = await main._fetch_light_meta(url)
        desc = await main._fetch_caption(url)
        out.append(f"\n## {i}. {m.get('title') or url}\n")
        out.append(f"- Link: {m.get('webpage_url') or url}")
        if m.get("uploader"): out.append(f"- Creator: {m['uploader']}")
        out.append(f"- Plattform: {main._platform_of(url)}")
        if m.get("upload_date"): out.append(f"- Datum: {m['upload_date']}")
        if desc: out.append(f"\n{desc}\n")
    return {"markdown": "\n".join(out)}


_LIVESTREAM_DESC = "Build a ready-to-run terminal command (yt-dlp/ffmpeg) to record a YouTube livestream on the user's Mac. mode: now (live from now), start (from stream start), section (part of a finished VOD, start/end as HH:MM:SS), merge (join interrupted parts), scan (snippet scan to find a position). quality: 1080 | 720 | 480 | audio."


async def get_livestream_script(url: str, mode: str = "now", quality: str = "1080",
                                name: str = "livestream", path: str = "~/Downloads",
                                start: str = "00:00:00", end: str = "00:05:00") -> dict:
    import main
    if not media_tools_enabled():
        return dict(_MEDIA_OFF)
    # The builder would quietly put a placeholder in; an assistant is better
    # served by an error it can act on.
    if err := _bad_url(url):
        return err
    return main.build_livestream_script(url, mode, quality, name, path, start, end)


_DOWNLOAD_DESC = "Build a ready-to-run local bash script (checks for yt-dlp/ffmpeg) that downloads the given URLs on the user's Mac and optionally merges them into one video. quality: best | 1080 | 720 | 480 | 360 | 240 | 144. merge=true joins all clips."


async def get_download_script(urls: list[str], quality: str = "best", audio_only: bool = False,
                              merge: bool = False, merge_resolution: str = "1080x1920",
                              cookies_from_browser: str = "") -> dict:
    import main
    if not media_tools_enabled():
        return dict(_MEDIA_OFF)
    # The builder drops non-http entries silently; say so instead, so the
    # assistant does not hand over a script missing half the links.
    for u in urls:
        if err := _bad_url(u):
            return {**err, "url": u}
    return {"script": main.build_download_script(list(urls), quality, audio_only, merge,
                                                 merge_resolution, cookies_from_browser=cookies_from_browser)}


# The functions exist either way (the web UI and tests call the builders
# directly); only what an assistant is offered depends on the operator.
# On the desktop they are always offered and gate themselves at call time.
if MEDIA_TOOLS_ENABLED or _DESKTOP:
    mcp.tool(description=_LIVESTREAM_DESC)(get_livestream_script)
    mcp.tool(description=_DOWNLOAD_DESC)(get_download_script)


# ── Podcasts ──────────────────────────────────────────────────────────────────
# These exist because the manual version was the bottleneck: nineteen episodes
# on 01. and 03.10. were each looked up by hand before any of the actual work
# could start. An assistant that already has the links in front of it can
# resolve them in one call.
#
# Resolution is cheap (a few HTTP requests, no audio) and is therefore separate
# from packaging, which downloads. That split lets an assistant check what it
# found — and report uncertain matches — before anything is fetched.

@mcp.tool(description="Resolve podcast links to episodes. Accepts Spotify, Apple Podcasts, RSS or show website links and returns per episode: show, title, date, duration, audio URL, shownotes and, if the show publishes one, the official transcript URL. Downloads nothing; call get_podcast_package afterwards. Each episode has status_code ('ok' | 'uncertain' | 'unresolved') and match ('guid' | 'dauer+datum' | 'titel'). 'uncertain' means matched by title only: a candidate, verify before quoting.")
async def resolve_podcast(items: list[str]) -> dict:
    import podcast
    episodes = await podcast.resolve_many(list(items))
    counts = {k: sum(1 for e in episodes if e.get("status") == k)
              for k in ("ok", "unsicher", "nicht_aufgeloest")}
    # The manifest is built from the untouched records, so status_code does
    # not leak into a file that is later fed back in as an order sheet.
    manifest = podcast.build_manifest(episodes)
    episodes = [{**e, "status_code": _status_code(e.get("status")),
                 "note": e.get("note") or ""} for e in episodes]
    return {
        "episodes": episodes,
        "summary": {
            **counts,
            "by_code": {code: sum(1 for e in episodes if e["status_code"] == code)
                        for code in ("ok", "uncertain", "unresolved")},
            "mit_offiziellem_transkript": sum(1 for e in episodes if e.get("transcripts")),
            "brauchen_whisper": sum(1 for e in episodes
                                    if e.get("status") != "nicht_aufgeloest"
                                    and not e.get("transcripts")),
        },
        "manifest": manifest,
    }


@mcp.tool(description="Get the shownotes and metadata of a single podcast episode as Markdown with a YAML header. Often the most useful part for research, since many shows link their sources there. Does not download audio.")
async def get_podcast_shownotes(url: str) -> dict:
    import podcast
    ep = await podcast.resolve(url)
    if ep.get("status") == "nicht_aufgeloest":
        return {"error": ep.get("note") or "Folge nicht gefunden.", "url": url,
                "status_code": "unresolved", "note": ep.get("note") or ""}
    return {
        "markdown": podcast.shownotes_markdown(ep),
        "status": ep.get("status"),
        "status_code": _status_code(ep.get("status")),
        "match": ep.get("match"),
        "note": ep.get("note", ""),
        "show": ep.get("show"),
        "title": ep.get("title"),
        "duration_s": ep.get("duration_s"),
        "has_official_transcript": bool(ep.get("transcripts")),
    }


@mcp.tool(description="Get the official transcript of a podcast episode if the show publishes one in its feed. format: 'text', 'timestamps' ([mm:ss] per line, best for quoting), 'segments', 'srt' or 'vtt'. If there is none, returns available=false; then use get_whisper_script (runs on the user's machine) or get_podcast_package on an instance with server transcription. For long texts set max_chars>0 and page with offset/next_offset.")
async def get_podcast_transcript(url: str, format: str = "text",
                                 offset: int = 0, max_chars: int = 0) -> dict:
    import main
    import podcast

    ep = await podcast.resolve(url)
    if ep.get("status") == "nicht_aufgeloest":
        return {"available": False, "error": ep.get("note") or "Folge nicht gefunden.",
                "status_code": "unresolved", "note": ep.get("note") or ""}
    tracks = ep.get("transcripts") or []
    if not tracks:
        return {"available": False,
                "reason": "Diese Sendung veröffentlicht kein Transkript im Feed.",
                "show": ep.get("show"), "title": ep.get("title"),
                "hint": "get_whisper_script liefert ein Script für den eigenen Rechner."}

    # Prefer a timed format: it can be reduced to plain text, not the reverse.
    track = (next((t for t in tracks if t.get("type") in ("vtt", "srt")), None)
             or tracks[0])
    raw = await podcast.fetch(track["url"], timeout=25)
    if not raw:
        return {"available": False, "error": f"Transkript nicht abrufbar: {track['url']}"}
    body = raw.decode("utf-8", errors="replace")

    if track.get("type") == "vtt":
        segments = main.vtt_to_segments(body)
    elif track.get("type") == "srt":
        # SRT differs from VTT only in the decimal separator for this purpose.
        segments = main.vtt_to_segments(body.replace(",", "."))
    else:
        segments = []

    if not segments:
        return _apply_page(
            {"available": True, "format": "text", "text": body.strip(),
             "source_url": track["url"], "source_type": track.get("type"),
             "note": "Unstrukturiertes Transkript, unverändert durchgereicht."},
            "text", offset, max_chars)

    out = {"available": True, "format": format, "source_url": track["url"],
           "source_type": track.get("type"), "show": ep.get("show"),
           "title": ep.get("title"), "segments_count": len(segments)}
    if format == "segments":
        out["segments"] = segments
    elif format == "timestamps":
        out["text"] = main.segments_to_timestamped(segments)
    elif format == "srt":
        out["text"] = main.segments_to_srt(segments)
    elif format == "vtt":
        out["text"] = main.segments_to_vtt(segments)
    else:
        out["text"] = main.segments_to_text(segments)
    return _apply_page(out, "text", offset, max_chars)


@mcp.tool(description="Build a package for several podcast episodes: per episode audio, shownotes, official transcript (also as Markdown), plus manifest.json and Whisper scripts for Mac and Windows for episodes without a transcript. items are links or an order sheet (format fundus-podcast/1). audio: 'klein' (mono speech, about 21 MB per hour), 'original' or 'nein' (none). Runs in the background; returns podcast_id. Poll get_podcast_package_status, then read files with read_podcast_package_file or search with search_podcast_transcripts.")
async def get_podcast_package(items: list[str], audio: str = "klein") -> dict:
    import asyncio
    import main

    uid = _uid()
    if not uid:
        return {"error": "Kein Nutzer. Bearer-Token prüfen."}
    if audio not in ("klein", "original", "nein"):
        return {"error": "audio muss klein, original oder nein sein."}
    if len(items) > main.PODCAST_MAX_EPISODES:
        return {"error": f"{len(items)} Folgen — höchstens {main.PODCAST_MAX_EPISODES}."}
    # Same budget as POST /api/podcast: one unit per episode download.
    if err := _throttled(len(items)):
        return err

    pid = str(uuid.uuid4())
    main.podcasts[pid] = {
        "status": "pending", "stage": "Wird vorbereitet…", "user_id": uid,
        "total": len(items), "done": 0,
        "created": main.datetime.now().isoformat(),
    }
    db.log_event(uid, "podcast")
    # Fire and report: downloading several episodes outlasts a tool call, and
    # a caller that wants to wait can poll the status URL.
    asyncio.create_task(main._podcast_job(pid, list(items), audio))
    return {
        "podcast_id": pid,
        "episodes_requested": len(items),
        "status_url": f"/api/podcast/{pid}",
        "zip_url": f"/api/podcast/{pid}/zip",
        "note": ("Läuft im Hintergrund. Status abfragen, bis status='done' — "
                 "erst dann ist die ZIP vollständig."),
    }


@mcp.tool(description="Build the Whisper script the user runs on their own computer to transcribe podcast episodes that have no official transcript. os_kind: 'mac' (zsh, mlx-whisper on Apple Silicon) or 'windows' (PowerShell). items are links or an order sheet. The script picks a model for the machine, splits episodes over 45 minutes and seeds Whisper with names from the shownotes.")
async def get_whisper_script(items: list[str], os_kind: str = "mac") -> dict:
    import main
    import podcast

    if os_kind not in ("mac", "windows"):
        return {"error": "os_kind muss mac oder windows sein."}
    episodes = await podcast.resolve_many(list(items))
    script = main.build_podcast_whisper_script(episodes, os_kind)
    if not script:
        return {"script": "", "note": "Alle Folgen haben ein offizielles Transkript im Feed."}
    need = [e.get("title") for e in episodes
            if e.get("status") != "nicht_aufgeloest" and not e.get("transcripts")]
    return {
        "script": script,
        "filename": "whisper_mac.command" if os_kind == "mac" else "whisper_windows.ps1",
        "episodes": need,
        "note": ("Das Script erwartet die Audiodateien in Unterordnern neben sich — "
                 "so wie get_podcast_package sie ablegt. Einmalig nötig: "
                 + ("brew install uv ffmpeg && uv tool install whisper-ctranslate2"
                    if os_kind == "mac"
                    else "winget install astral-sh.uv Gyan.FFmpeg")),
    }


mcp_app = mcp.streamable_http_app()


@mcp.tool(description="Search the transcripts of a finished podcast package: answers 'which episode talked about X, and from when'. Per hit: episode, title, timestamp, passage, a ready citation (cite) and a link. Several words are combined with AND. podcast_id comes from get_podcast_package. Searches only this package; there is no index of earlier runs.")
async def search_podcast_transcripts(podcast_id: str, query: str,
                                     limit: int = 40) -> dict:
    import asyncio
    import main
    import podcast as pc

    rec, err = _own_record(podcast_id)
    if err:
        return err
    folder = main._podcast_dir(podcast_id)
    if not folder.exists():
        return {"error": "Paket nicht mehr vorhanden — Aufbewahrungsfrist abgelaufen."}

    hits = await asyncio.to_thread(
        main.search_transcripts, folder, query, max(1, min(int(limit or 40), 200)),
        rec.get("episodes"))
    titles = {pc.episode_slug(ep): ep.get("title", "")
              for ep in (rec.get("episodes") or []) if ep.get("title")}
    for h in hits:
        h["title"] = titles.get(h["episode"], h["episode"])
    untimed = any(not h["at"] for h in hits)
    return {"query": query, "count": len(hits), "hits": hits,
            "hinweis": ("Zeitmarken gibt es nur, wo ein Transkript mit Zeiten vorliegt. "
                        "Bei reinem Fließtext ist »at« leer — keine erfundene Zeit.")
            if untimed else "",
            "status_code": "uncertain" if untimed else "ok",
            "note": ("Timestamps exist only where a timed transcript is available. "
                     "For plain text 'at' is empty, no time is invented.")
            if untimed else ""}


def _own_record(podcast_id: str) -> tuple[dict | None, dict | None]:
    """The package record, or the error to hand back.

    Eigentum prüfen, sonst wäre jedes Paket-Tool ein Weg an die Dateien
    fremder Läufe — die podcast_id ist eine UUID, aber das ist keine
    Berechtigung. Unknown and foreign ids get the same answer so the reply
    does not confirm that someone else's run exists.
    """
    import main
    uid = _uid()
    if not uid:
        return None, {"error": "Kein Nutzer. Bearer-Token prüfen."}
    rec = main.podcasts.get(podcast_id)
    if not rec or rec.get("user_id") != uid:
        return None, {"error": "Unbekannte podcast_id."}
    return rec, None


def _has_transcript(folder: Path, slug: str) -> bool:
    return any((folder / slug).glob(f"{slug}_Transkript.*"))


@mcp.tool(description="Status of a podcast package from get_podcast_package: status ('pending' | 'running' | 'done' | 'error'), stage, done/total, and per episode title, show, status_code ('ok' | 'uncertain' | 'unresolved') and whether a transcript exists. Poll this until status is 'done' instead of downloading the ZIP.")
async def get_podcast_package_status(podcast_id: str) -> dict:
    import main
    import podcast as pc

    rec, err = _own_record(podcast_id)
    if err:
        return err
    folder = main._podcast_dir(podcast_id)
    episodes = []
    for ep in rec.get("episodes") or []:
        unresolved = ep.get("status") == "nicht_aufgeloest"
        slug = "" if unresolved else pc.episode_slug(ep)
        episodes.append({
            "episode": slug,
            "title": ep.get("title") or "",
            "show": ep.get("show") or "",
            "status": ep.get("status"),
            "status_code": _status_code(ep.get("status")),
            "note": ep.get("note") or "",
            # Checked on disk, not inferred from the feed: a listed transcript
            # whose download failed is exactly the case a caller must see.
            "has_transcript": bool(slug) and _has_transcript(folder, slug),
        })
    out = {"podcast_id": podcast_id, "status": rec.get("status"),
           "stage": rec.get("stage") or "", "done": rec.get("done", 0),
           "total": rec.get("total", 0), "episodes": episodes}
    if rec.get("error"):
        out["error"] = rec["error"]
    return out


_PACKAGE_KINDS = ("transcript", "shownotes", "manifest")


@mcp.tool(description="Read a file from a podcast package without downloading the ZIP. episode is the folder name from get_podcast_package_status (field 'episode'). kind: 'transcript', 'shownotes' or 'manifest' (episode not needed). Returns up to max_chars characters from offset plus next_offset for the next call (null = end).")
async def read_podcast_package_file(podcast_id: str, episode: str = "",
                                    kind: str = "transcript", offset: int = 0,
                                    max_chars: int = 20000) -> dict:
    import main
    import podcast as pc

    rec, err = _own_record(podcast_id)
    if err:
        return err
    max_chars = max(1, min(int(max_chars or 20000), 100_000))   # one page, never the whole file
    if kind not in _PACKAGE_KINDS:
        return {"error": f"kind muss {', '.join(_PACKAGE_KINDS)} sein."}
    root = main._podcast_dir(podcast_id).resolve()
    if not root.is_dir():
        return {"error": "Paket nicht mehr vorhanden — Aufbewahrungsfrist abgelaufen."}

    if kind == "manifest":
        path = root / "manifest.json"
    else:
        # Only slugs the record itself produced are accepted. The name comes
        # from the caller, and "../../" or an absolute path must not become a
        # way to read files outside this package.
        slugs = {pc.episode_slug(ep) for ep in (rec.get("episodes") or [])
                 if ep.get("status") != "nicht_aufgeloest"}
        if episode not in slugs:
            return {"error": "Unbekannte Folge in diesem Paket.",
                    "episodes": sorted(slugs)}
        folder = root / episode
        if kind == "shownotes":
            path = folder / f"{episode}_Shownotes.md"
        else:
            # Plain text first, it is what people read; timed formats after.
            path = next((folder / f"{episode}_Transkript.{ext}"
                         for ext in ("txt", "vtt", "srt", "json", "html")
                         if (folder / f"{episode}_Transkript.{ext}").is_file()),
                        folder / f"{episode}_Transkript.txt")

    # Second line of defence: whatever the name, the resolved file (symlinks
    # included) has to sit inside the package directory.
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        return {"error": "Pfad außerhalb des Pakets."}
    if not resolved.is_file():
        return {"error": "Datei nicht vorhanden.", "kind": kind, "episode": episode,
                "status": rec.get("status")}

    body = resolved.read_text(encoding="utf-8", errors="replace")
    text, next_offset = _page(body, offset, max_chars)
    return {"podcast_id": podcast_id, "episode": episode if kind != "manifest" else "",
            "kind": kind, "file": resolved.name, "offset": max(0, int(offset or 0)),
            "total_chars": len(body), "text": text, "next_offset": next_offset}


# ── Messages in the instance language ─────────────────────────────────────────
# Error texts and notes are written in German where they arise (they are shared
# with the web UI). An MCP request carries no language choice, and the tool
# descriptions are English, so results go out in the instance's default
# language — English unless the operator set FUNDUS_LANG. Applied to the
# registered tools only: what a client calls. The Python functions themselves
# stay untouched, so other code calling them gets the source text.
_MCP_MESSAGE_FIELDS = ("error", "note", "stage", "detail", "hint")


def _localize_tool(fn):
    import functools
    import i18n

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return i18n.translate_fields(await fn(*args, **kwargs), i18n.DEFAULT_LANG,
                                     _MCP_MESSAGE_FIELDS)
    return wrapper


for _tool in mcp._tool_manager.list_tools():
    if getattr(_tool, "is_async", False) and not getattr(_tool.fn, "_localized", False):
        _tool.fn = _localize_tool(_tool.fn)
        _tool.fn._localized = True
