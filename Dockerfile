FROM python:3.11-slim

# ffmpeg      — merging, audio extraction, trimming
# unzip/curl  — fetching the Deno runtime below
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── JavaScript runtime ────────────────────────────────────────────────────────
# yt-dlp needs an external JavaScript engine for some sites. The image ships
# the runtime, not the extractor: Deno does nothing on its own, and without an
# extractor installed it is never invoked. Deno is the runtime yt-dlp enables
# by default, and the one that sandboxes page scripts away from the filesystem
# and network.
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh -s -- --yes \
    && deno --version

# ── Unprivileged user ─────────────────────────────────────────────────────────
# The server runs untrusted input through yt-dlp, gallery-dl and ffmpeg. As
# root, any bug in one of them is root in the container — and with a mounted
# volume or a kernel bug, a step closer to the host. A fixed uid (10001) so
# volume ownership is predictable across rebuilds and can be fixed from the
# host by number.
#
# Packages go into a virtualenv the app user owns, not into the system
# Python: the entrypoint (EXTRACTOR_AUTO_INSTALL) and the self-update in the
# System tab both pip-install at runtime, and as a non-root user they could
# not write to /usr/local. PATH puts the venv first, so `python3`, `pip`,
# `yt-dlp` and `gallery-dl` all resolve there, and sys.executable -m pip in
# _self_update targets the same environment.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app \
               --shell /usr/sbin/nologin app \
    && python -m venv /opt/venv \
    && chown -R app:app /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

COPY requirements.txt .
# Installed as app, so later runtime installs can upgrade these in place.
USER app
RUN pip install --no-cache-dir -r requirements.txt

# ── What is deliberately NOT in this image ────────────────────────────────────
# No media extractor (yt-dlp, gallery-dl) and no extractor plugins. An image
# that contains them distributes the tool rather than linking to it, and that
# is a choice belonging to whoever runs this, not to whoever builds it.
#
# Set EXTRACTOR_AUTO_INSTALL=1 and the entrypoint installs
# requirements-extractors.txt on start; or mount your own binary. Transcript
# retrieval via the subtitle endpoint and podcasts work without it either way.
RUN pip install --no-cache-dir --upgrade youtube-transcript-api

USER root
COPY . .

# The code stays root-owned and read-only for the app user: a compromised
# process should not be able to rewrite the server it runs as. Only the
# directories it legitimately writes to belong to it. /app itself too,
# because main.py creates these relative to the working directory and a
# missing one (e.g. after a bind mount over it) must be creatable.
RUN mkdir -p downloads tmp data models \
    && chmod +x docker-entrypoint.sh \
    && chown app:app /app \
    && chown -R app:app downloads tmp data models

ENV PORT=8000 \
    WHISPER_MODEL_DIR=/app/models \
    HOME=/home/app
EXPOSE 8000

# The entrypoint starts as root only to fix ownership of volumes created by
# older images (which ran as root), then drops to "app" before running
# anything else. Without that step an update left existing installs with a
# data directory the server could not write.
USER root

# Only the server is checked: the extractor is optional, and an instance
# running transcripts alone is healthy.
HEALTHCHECK --interval=5m --timeout=20s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
