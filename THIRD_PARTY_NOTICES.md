# Third-party components

Fundus itself is licensed under AGPL-3.0-only. It depends on, or can install,
the components below. Fundus does not modify any of them.

## Python libraries imported by Fundus (`requirements.txt`)

| Component | License | Use |
|---|---|---|
| FastAPI | MIT | web framework |
| Starlette | BSD-3-Clause | via FastAPI |
| Uvicorn | BSD-3-Clause | ASGI server |
| Jinja2 | BSD-3-Clause | templates |
| python-multipart | Apache-2.0 | form parsing |
| aiofiles | Apache-2.0 | async file I/O |
| mcp (Model Context Protocol SDK) | MIT | MCP server |
| youtube-transcript-api | MIT | caption retrieval |
| faster-whisper | MIT | local transcription |
| CTranslate2 | MIT | via faster-whisper |
| PyAV (av) | BSD-3-Clause; binary wheels bundle FFmpeg libraries (LGPL-2.1+) | via faster-whisper |
| curl_cffi | MIT | HTTP client |
| certifi | MPL-2.0 | CA bundle (transitive) |

## Separate programs — run as subprocesses, never imported

Installed by whoever runs Fundus (`requirements-extractors.txt`). The
published container image does not contain them.

| Component | License | Use |
|---|---|---|
| yt-dlp (+ yt-dlp-ejs) | Unlicense (ejs: Unlicense AND MIT AND ISC) | media and metadata extraction, run as `python -m yt_dlp` |
| gallery-dl | GPL-2.0-only | image posts, run as a separate process. **Must never be imported into Fundus code** — GPL-2.0-only cannot be combined with AGPL-3.0. |
| mutagen | GPL-2.0-or-later | optional yt-dlp dependency |
| FFmpeg / ffprobe | LGPL-2.1+ or GPL-2.0+, depending on the build | merging, trimming, audio extraction |
| Deno | MIT | JavaScript runtime used by yt-dlp |

## macOS app only — installed on first launch, not bundled

| Component | License | Use |
|---|---|---|
| mlx-whisper, MLX | MIT | transcription on Apple Silicon |
| static-ffmpeg | MIT (wrapper; downloads FFmpeg builds, see above) | provides ffmpeg if missing |
| deno (PyPI) | MIT | JavaScript runtime |

## Optional, operator-enabled

| Component | License | Use |
|---|---|---|
| Whisper model weights (faster-whisper / mlx-community conversions) | per model card; OpenAI Whisper weights are MIT | downloaded at first use from Hugging Face |

## Fonts — bundled in `fonts/`

| Font | License |
|---|---|
| Space Grotesk | SIL Open Font License 1.1 — `fonts/OFL-SpaceGrotesk.txt` |
| Space Mono | SIL Open Font License 1.1 — `fonts/OFL-SpaceMono.txt` |

## Container image

Published images are based on `python:3.11-slim` (Debian) and contain Debian
packages, including ffmpeg, under their respective licences. The
corresponding source for each package is available from Debian
(`apt-get source <package>` for the version in the image, or
[sources.debian.org](https://sources.debian.org)). On request, the maintainer
will provide the corresponding source for any image published from this
repository for three years after its release.
