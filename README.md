<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/wordmark-dark.svg">
    <img src="assets/brand/wordmark.svg" alt="Fundus" height="64">
  </picture>
</h1>

[Deutsch](README.de.md)

**Fundus turns podcasts and videos into text your AI can work with.**
It makes a transcript out of a recording and a searchable episode package out
of a podcast link, and it hands both to Claude or any other assistant over MCP.
Self-hosted, open source, no public instance.

[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Docker%20%7C%20macOS-lightgrey.svg)](#running-it)

---

Most of what gets said in podcasts and videos never ends up as text. You
remember that someone said something smart about pricing three weeks ago, in
one of ten episodes, somewhere after the first half hour, and to find it again
you have to listen again.

AI assistants are good at reading and quoting, as long as they have the text.
Getting it to them usually means copying captions by hand or paying a
transcription service by the minute.

I built Fundus for my own podcast research. You paste a link (or ten) and get
transcripts with timestamps. Fundus uses published captions where they exist,
the show's own transcript if a podcast publishes one, and Whisper on your own
hardware if neither is available. After that, your assistant can fetch and
search the text itself.

## Who it's for

- Researchers who work with interviews, talks and lectures and need text they
  can cite, with timestamps.
- Journalists who want to check what was actually said, and when, before they
  quote it.
- Podcast listeners who want to search a whole show instead of scrubbing
  through hours of audio.
- Anyone working with AI assistants who wants transcripts and podcast episodes
  available as MCP tools instead of copy and paste.

## What it does

**Transcripts.** Published captions if a platform provides them (YouTube
captions need nothing extra). Where there are none, as with most reels and
short videos, Fundus fetches the audio and transcribes it locally with
Whisper (faster-whisper on servers, MLX on Apple Silicon Macs). Fetching that
audio needs the optional media extractor, and for YouTube also the YouTube
switch (see [below](#optional-media-extractors)). Your own audio and video
files can always be uploaded and transcribed. Timestamps are clickable, you
see the word count, and you can search within the text. Export as `txt`, `md`,
`srt`, `vtt` or `json`.

**Podcasts.** Paste Spotify, Apple Podcasts, RSS or show website links, and
Fundus finds the matching feed entry for each one: show, title, date,
duration, shownotes and the official transcript, if the show publishes one.
It matches by GUID first, then by duration and date. Matching by title only is
the last resort, and those episodes are marked as uncertain. Many shows
publish no transcript at all. The macOS app transcribes those episodes
itself. A server writes a ready-to-run Whisper script instead (macOS with MLX,
or Windows PowerShell) that transcribes them on your own computer, unless the
operator sets `PODCAST_SERVER_WHISPER=1`. Names from the shownotes are passed to Whisper
so it spells them correctly.

**Order sheets (Auftragszettel).** For many episodes at once, an assistant can
prepare an order sheet, a small structured list in the `fundus-podcast/1`
format. Fundus turns it into one package with a `manifest.json`, shownotes as
Markdown, official transcripts where available, optional speech-quality audio
and the Whisper script for everything else. The schema an assistant needs is
served at `/api/podcast/auftragszettel.md`.

**Search across episodes.** Inside a podcast package you can search all
transcripts at once. You get back the episode, the timestamp and the passage,
so you know which episode talked about X and from when. This works through
MCP and the API. Search only covers the package you are working with; there
is no index across packages.

**MCP server.** An `/mcp` endpoint with a personal bearer token for each user,
so Claude or another assistant can fetch transcripts, resolve podcasts, build
packages and search them without you in between. Details [below](#mcp).

**Posts and threads.** Text posts and threads are saved as Markdown, together
with any attached images.

**Audio trimmer.** A waveform in the browser with two handles, a preview and a
clean cut with a short fade. Handy for taking the intro off a recording
without opening an audio editor.

**Bulk paste.** Paste a list of links and see what is happening with each one.
Duplicates are flagged before anything runs.

**Two languages.** The interface is in German and English. It follows your
browser language, and you can switch at any time.

**Optional media retrieval.** Fundus can also save the media files behind a
link (videos, photos, audio), for content you are allowed to copy. This only
works through third-party programs (yt-dlp, gallery-dl) that the person
running the instance installs. The published container image does not contain
them, and in the macOS app it is an explicit opt-in. See
[Optional: media extractors](#optional-media-extractors).

## Honest limits

- **Datacenter IPs get blocked.** Some video platforms treat requests from
  rented servers as suspicious and refuse to hand out captions, while home
  connections count as normal. Nothing in the software changes that. If you
  run into it, run Fundus where you are (the macOS app, or a machine on a home
  connection), or rely on official podcast transcripts and Whisper, which
  don't depend on a platform handing out captions.
- **Whisper on a small server is slow.** The `base` model on four cores runs
  at roughly a tenth of real time and is noticeably weaker on German. Apple
  Silicon can handle the large model.
- **Podcast matching is not always certain.** Episodes matched by title only
  are marked `unsicher` (uncertain). Check them before you quote them.
- **Files are temporary.** A server deletes fetched files after
  `CLEAN_AGE_HOURS` (6 hours by default, 7 days in the app). Fundus is meant
  for working with the text, it doesn't try to be an archive.

## Running it

You can run the same code in two ways: as a container on a server you
control, or as a native macOS app that runs entirely on your Mac. There is no
public instance.

### Docker

```bash
git clone https://github.com/tillheidrich/fundus.git
cd fundus
cp .env.example .env      # adjust what you need
docker compose up -d
```

Open `http://localhost:8000` and create the first account. It becomes the
administrator, and sign-up closes after that unless you set `SIGNUP_CODE`
(invite code) or `OPEN_SIGNUP=1`.

Out of the box the container handles YouTube captions, podcasts with their
official transcripts, and Whisper for files you upload. The server process
runs as an unprivileged user and the port is bound to `127.0.0.1`. Whisper
models are downloaded from Hugging Face the first time you use them and kept
in their own volume.

#### Optional: media extractors

The image ships without a media extractor. Whether you add one is your
decision as the operator, so it is off by default. Either set

```bash
EXTRACTOR_AUTO_INSTALL=1
```

in `.env`, which installs yt-dlp and gallery-dl on first start, or mount your
own binary at `/usr/local/bin/yt-dlp`. With it, Fundus can read subtitles from
other platforms, transcribe reels and short videos that have none, and save
media files. Media retrieval from YouTube, including audio for transcription,
stays off until you also set `ENABLE_YOUTUBE_VIDEO=1`.

### macOS

There is no `.dmg` to download. The installer below fetches the source of the
newest release, builds the app on your Mac and puts it in Applications. Paste
it into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/tillheidrich/fundus/main/macos/install.sh | bash
```

What it does, in order: checks for macOS 13+ and Apple's Command Line Tools
(if they are missing, macOS shows a dialog to install them, about 1–2 GB;
run the line again afterwards), clones the newest release tag, compiles the
app, signs it for this Mac and installs it to `/Applications` (or
`~/Applications` if that is not writable). Since the app was built here and
not downloaded, Gatekeeper shows no warning. Building takes a few minutes the
first time.

Prefer to read a script before running it? Download it first:

```bash
curl -fsSLO https://raw.githubusercontent.com/tillheidrich/fundus/main/macos/install.sh
less install.sh && bash install.sh
```

Options: `--ref v1.1.0` builds a specific version, `--user` installs to
`~/Applications`, `--uninstall` removes the app and the build sources (your
data stays). With the pipe, pass them as `… | bash -s -- --user`.

By hand, if you'd rather:

```bash
git clone https://github.com/tillheidrich/fundus.git
cd fundus/macos && ./build-app.sh --install
```

Signing is optional: without a signing identity the script signs the app ad
hoc, which is all it needs to run on the Mac that built it.

The app runs entirely on your Mac. The server only listens on the loopback
interface, there is no login and no telemetry. The only outbound connections
are the requests you make and update checks (listed in
[docs/PRIVACY.md](docs/PRIVACY.md)). On first launch it sets up what it needs
under `~/Library/Application Support/Fundus` (a Python runtime, ffmpeg and,
on Apple Silicon, MLX Whisper) and shows you what it is doing. That takes a
couple of minutes, once. If you already have ffmpeg from Homebrew, the app
uses that one. It installs no media extractor during setup.

The app installs no media extractor without your explicit consent. Fundus
itself downloads no media; under System → Media you can have it install the
open-source yt-dlp (plus the JavaScript runtime it needs) from PyPI, after
ticking a consent box. Downloading YouTube videos is off by default and has
its own switch and acknowledgement, as do the download tools for AI
assistants. YouTube transcripts (captions) always work without either.

Your data lives outside the app bundle, so when you replace the app with a
newer build, your settings and files stay where they are.

## Updating

**Docker, from the published image.** `docker-compose.yml` points at
`ghcr.io/tillheidrich/fundus:latest`, so this is enough:

```bash
docker compose pull && docker compose up -d
```

**Docker, from source.**

```bash
git pull && docker compose up -d --build
```

**macOS app.** Run the install command again. It builds the newest release
and replaces the app; settings and files in
`~/Library/Application Support/Fundus` are untouched. The app checks once a
day for a newer release tag on GitHub (no user data is sent) and, if there is
one, offers to copy that command and open Terminal for you. If you built by
hand: `git pull && cd macos && ./build-app.sh --install`.

**yt-dlp and the other components.** Use the update button in the System
section (on a server, that is on an administrator's account page). Automatic
updates are off by default, `UPDATE_INTERVAL_H` turns them on. Once a day
Fundus checks PyPI for new versions, and that check installs nothing.

## Configuration

All settings are environment variables. `.env.example` lists every one of them
with an explanation. These are the ones most people change:

| Variable | Default | |
|---|---|---|
| `SIGNUP_CODE` | unset | Invite code. If unset, sign-up closes after the first account |
| `OPEN_SIGNUP` | `0` | Let anyone register. Rarely what you want |
| `WHISPER_ENABLED` | `1` | Local transcription when no captions exist |
| `WHISPER_MODEL` | `base` | `tiny` · `base` · `small` · `medium` · `large-v3-turbo` |
| `WHISPER_MAX_MINUTES` | `45` | Longest recording the server transcribes |
| `PODCAST_MAX_EPISODES` | `25` | Episodes per podcast package |
| `PODCAST_SERVER_WHISPER` | `0` | Transcribe podcast episodes on the server instead of handing over a script (runs limited by `WHISPER_CONCURRENCY`, default `1`) |
| `CLEAN_AGE_HOURS` | `6` | Fetched files are deleted after this |
| `BATCH_MAX_URLS` | `150` | Links per batch |
| `TRIM_MAX_MINUTES` | `180` | Longest recording the trimmer accepts |
| `EXTRACTOR_AUTO_INSTALL` | `0` | Install yt-dlp and gallery-dl on first start |
| `ENABLE_YOUTUBE_VIDEO` | `0` | Allow media retrieval from YouTube (needs an extractor) |
| `ENABLE_MEDIA_TOOLS` | `0` | Offer the MCP tools `get_download_script` and `get_livestream_script` |
| `UPDATE_INTERVAL_H` | `0` | Hours between automatic component updates. `0` disables |
| `CHECK_INTERVAL_H` | `24` | Hours between version checks. `0` disables |
| `TRUSTED_PROXIES` | unset | Addresses of reverse proxies in front of the instance |
| `APP_NAME` · `BRAND_TAG` | `Fundus` | Your own wordmark, if you want one |
| `FUNDUS_LANG` | `en` | Fallback language when the browser asks for neither German nor English |
| `TZ` | `UTC` | |

## Reverse proxy

Put TLS in front of the instance and tell Fundus which proxy to trust, so it
sees the real client addresses:

```bash
TRUSTED_PROXIES=127.0.0.1
```

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 600s;   # transcription is not a fast request
}
```

Setting `TRUSTED_PROXIES` also turns off the desktop login exemption, so an
instance behind a proxy always asks for a login. Sign-up is closed by default,
but an instance on the open internet should still sit behind authentication
you control.

## MCP

Fundus runs a Model Context Protocol server at `/mcp` (streamable HTTP). Each
user has a personal bearer token, which the account page shows together with
the endpoint URL.

Tested with Claude Code, Codex and the `mcp-remote` bridge; any client that
speaks streamable HTTP should work, including local clients for open models.
The app's System page (account page on a server) shows a snippet for each
client with your token filled in. In short:

**Claude Code**

```bash
claude mcp add --transport http fundus https://fundus.example.com/mcp \
  --header "Authorization: Bearer YOUR_TOKEN" --scope user
```

**Codex** (CLI, app and IDE share `~/.codex/config.toml`)

```toml
[mcp_servers.fundus]
url = "https://fundus.example.com/mcp"
bearer_token_env_var = "FUNDUS_TOKEN"
```

Codex asks before each MCP tool call. Interactively that is one click; in
non-interactive `codex exec` the call is refused unless approvals are
switched off for that run.

**LM Studio, Cline, AnythingLLM, Cursor** and other clients that read an
`mcpServers` block with a URL (Cline also wants `"type": "streamableHttp"`,
AnythingLLM `"type": "streamable"`):

```json
{
  "mcpServers": {
    "fundus": {
      "url": "https://fundus.example.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

**Goose**: an extension of `type: streamable_http` with the same URL and an
`Authorization` header. **Open WebUI**: Admin settings, External Tools, type
"MCP (Streamable HTTP)", auth Bearer.

**Claude Desktop, Jan, Msty, oterm** and anything else that only starts local
(stdio) servers: use the `mcp-remote` bridge, which needs Node.

```json
{
  "mcpServers": {
    "fundus": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8765/mcp",
               "--header", "Authorization:${FUNDUS_AUTH}"],
      "env": { "FUNDUS_AUTH": "Bearer YOUR_TOKEN" }
    }
  }
}
```

The macOS app serves MCP at `http://127.0.0.1:8765/mcp` (if another program
holds that port, it picks a free one and shows it on the System page). Claude's and
ChatGPT's own cloud connectors can't reach a local address and only offer
OAuth, so they don't work with Fundus yet; the clients above do. Tool names
and descriptions are in English, which helps smaller open models pick the
right tool.

Main tools:

| Tool | |
|---|---|
| `get_transcript(url, lang, format)` | Transcript as `text`, `timestamps`, `segments`, `srt` or `vtt` |
| `list_transcript_languages(url)` | Available caption tracks |
| `get_caption(url)` | The written description of a post |
| `get_metadata(urls[])` | Title, link, creator, date and description for several items, as Markdown |
| `resolve_podcast(items[])` | Podcast links to episodes, with match quality and official transcript URLs |
| `get_podcast_shownotes(url)` | Shownotes and metadata of one episode as Markdown |
| `get_podcast_transcript(url, format)` | The show's official transcript, if it publishes one |
| `get_podcast_package(items[], audio)` | A package for many episodes or an order sheet |
| `get_whisper_script(items[], os_kind)` | A local Whisper script for episodes without a transcript |
| `search_podcast_transcripts(podcast_id, query)` | Which episode said what, and from which timestamp |

Treat the token like a password. Anyone who has it can act as you.

## Privacy

Fundus has no telemetry and no analytics. [docs/PRIVACY.md](docs/PRIVACY.md)
lists what it stores, for how long, and which outbound connections it opens,
so operators can write their own privacy notice. One thing to know: platform
cookies and API tokens are stored unencrypted in the database, so protect the
data volume and its backups.

## Development

```bash
pip install -r requirements.txt
pytest                                 # tests, no network
uvicorn main:app --reload
tools/smoke.sh http://127.0.0.1:8000   # end-to-end, hits the network
```

Python 3.10 or newer, FastAPI, Jinja2, faster-whisper and the MCP Python SDK.
The macOS shell is written in Swift. [CONTRIBUTING.md](CONTRIBUTING.md) covers
scope, conventions and what won't be merged. Problems with an extractor
belong upstream at [yt-dlp](https://github.com/yt-dlp/yt-dlp/issues).

Security issues: please write to mail@tillheidrich.de instead of opening a
public issue.

## Legal

**Intended use.** Fundus retrieves transcripts, captions, metadata and media
that you are entitled to access: your own uploads, openly licensed works,
material you have permission to use, or uses covered by a statutory exception
where you live (quotation, reporting, research, accessibility). It is not a
tool for obtaining copies of works you have no right to copy.

**You are responsible.** Fundus is software you run yourself; there is no
public instance. Whoever operates an instance decides what it fetches and is
responsible for complying with copyright law, the terms of service of the
platforms involved, and data-protection law towards their users.

**Technical protection measures.** Fundus contains no code for defeating encryption or DRM
and does not advertise or support defeating technical protection measures. Media retrieval relies on third-party programs (yt-dlp,
gallery-dl). The published container image does not contain them; you install
them yourself, or the macOS app installs yt-dlp when you ask it to, after
explicit consent.
In some jurisdictions, using or distributing tools that defeat technical
measures is restricted, e.g. by § 95a of the German Copyright Act (UrhG) or
17 U.S.C. § 1201 (DMCA). German courts have held YouTube's URL signature to be
an effective technical measure (OLG Hamburg, 5 U 54/23), and the Federal Court
of Justice declined to hear the appeal in October 2025. The macOS app installs
no extractor without your explicit consent, and YouTube video download is off
by default. Check the law that applies to you.
**This section is not legal advice.**

**No affiliation.** Fundus is an independent project and is not affiliated
with, endorsed by, or sponsored by YouTube/Google, Instagram/Threads/Meta,
TikTok/ByteDance, Spotify or Apple. Product names are the property of their
owners and are used only to describe compatibility. Pull requests adding
support for services dedicated to infringement will be rejected.

**Warranty.** Provided "as is", without warranty, as set out in sections 15
and 16 of the AGPL-3.0.

## License

[AGPL-3.0-only](LICENSE). Third-party components and their licences are
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md); the container image
contains Debian's ffmpeg build, whose corresponding source is available from
Debian.

Fundus is written by Till Heidrich.
