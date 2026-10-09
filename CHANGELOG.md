# Changelog

## Unreleased

- **One-line installer for macOS.** `macos/install.sh` builds the newest
  release from source and installs it to Applications; run it again to
  update, `--uninstall` to remove. No disk image involved.
- **Update prompt without a disk image.** When a newer release exists, the
  app offers to copy the install command and open Terminal instead of
  pointing to a release page with nothing to download.
- The project website is gone; this repository is the home page.

## 1.1.0

**Media download is opt-in on the Mac.** The app no longer installs yt-dlp,
gallery-dl or the Deno runtime on first launch. Under System → Media it
installs them from PyPI only after an explicit consent, which is recorded.
YouTube video download and the MCP download tools (`get_download_script`,
`get_livestream_script`) are off by default on the desktop and each needs its
own acknowledgement to switch on; the MCP tools stay listed and return an
error while off. Existing installs keep their yt-dlp. YouTube transcripts
work as before. Servers are unchanged: `ENABLE_YOUTUBE_VIDEO` and
`ENABLE_MEDIA_TOOLS` still decide there.

- **Media opt-in in the app.** Nothing that downloads media is installed
  without a consent dialog.
- **YouTube video download off by default**, on the desktop and on servers.
- **New brand and icon.** New wordmark and app icon.
- **English UI complete.** The interface is fully available in English as
  well as German.
- **MCP compatibility.** Tested setups and copyable snippets for Claude Code,
  Codex, LM Studio, Open WebUI, Goose and, for stdio-only clients,
  `mcp-remote`.
- **Markdown export** for transcripts.
- **Transcribe your own files.** Upload a local audio or video file and get a
  Whisper transcript.
- **Security hardening.** The container runs as a non-root user, response
  headers are tightened, and SSRF and option-injection issues are fixed.
- **Distribution.** The GitHub repository and the Docker image are public.
  The macOS app is not distributed publicly; build it from source with
  `macos/build-app.sh`.

## 1.0.0 — first public release

**Transcripts.** Published captions where available, local Whisper
(faster-whisper on servers, MLX on Apple Silicon) where not. Search,
clickable timestamps, export as txt, md, srt, vtt, json.

**Podcasts.** Spotify, Apple Podcasts, RSS and show-website links resolved to
the feed entry (GUID, then duration and date, title only as a marked last
resort). Official transcripts where published, otherwise a ready-to-run
Whisper script for macOS or Windows. Order sheets (`fundus-podcast/1`) for
many episodes at once; full-text search across a package.

**MCP.** `/mcp` endpoint with a personal bearer token: transcripts,
captions, metadata, podcast resolution, packages, Whisper scripts and
transcript search as tools for Claude and other assistants.

**macOS app.** Signed and notarised. Runs the same server locally on port
8765, installs and updates its own components, history with private mode,
"check for new episodes" for podcasts already fetched, optional clipboard
watching (opt-in, asks before adding anything).

**Also.** Audio trimmer with waveform, posts and threads as Markdown, bulk
paste with link preview, audio format choice (MP3, M4A, Opus, original),
German and English interface.

**Self-hosting.** Docker image without bundled extractors; the operator
opts in with `EXTRACTOR_AUTO_INSTALL=1`. No font CDN, no telemetry.
