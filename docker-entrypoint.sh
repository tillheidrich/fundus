#!/bin/sh
# Prepares optional components, then starts the server.
#
# Why the media extractor is not baked into the image: shipping a container
# that already contains it means distributing the tool, not linking to it.
# Whether to add it is the operator's call, made once, explicitly — and this
# script is where that choice takes effect.
#
#   EXTRACTOR_AUTO_INSTALL=1   install requirements-extractors.txt on start
#   EXTRACTOR_REFRESH=1        reinstall even if already present
#   (unset)                    run without them; transcripts still work

set -eu

# Root phase, done once and only here: volumes created by an older image
# (which ran as root) are handed to the unprivileged user "app" (uid 10001),
# then the script restarts itself as that user. Nothing else ever runs as
# root: not pip, not yt-dlp, not the server.
if [ "$(id -u)" = "0" ]; then
    for d in /app/data /app/models /app/downloads /app/tmp; do
        mkdir -p "$d"
        if [ "$(stat -c %u "$d")" != "10001" ]; then
            chown -R 10001:10001 "$d" || echo "› Konnte $d nicht übernehmen (chown)." >&2
        fi
    done
    exec setpriv --reuid=10001 --regid=10001 --init-groups "$0" "$@"
fi

have() { command -v "$1" >/dev/null 2>&1 || python3 -c "import $2" >/dev/null 2>&1; }

if [ "${EXTRACTOR_AUTO_INSTALL:-0}" = "1" ]; then
    # Checked against what is installed, not against a marker file: the data
    # volume outlives the container, so a marker would say "done" on a fresh
    # image that has nothing in it.
    if ! have yt-dlp yt_dlp || ! have gallery-dl gallery_dl || [ "${EXTRACTOR_REFRESH:-0}" = "1" ]; then
        echo "› Extraktoren werden geladen (EXTRACTOR_AUTO_INSTALL=1)…"
        # --pre only for yt-dlp itself (its nightlies follow site changes
        # fastest); everything else stays on stable releases.
        python3 -m pip install --no-cache-dir --quiet --upgrade \
            -r /app/requirements-extractors.txt \
            && python3 -m pip install --no-cache-dir --quiet --upgrade --pre "yt-dlp[default]" \
            && echo "  fertig." \
            || echo "  fehlgeschlagen — der Server startet trotzdem."
    fi
else
    if ! have yt-dlp yt_dlp; then
        cat <<'MSG'
› Kein Medien-Extraktor installiert.

  Transkripte über die Untertitel-Schnittstelle und Podcasts funktionieren
  bereits. Für alles Weitere stelle yt-dlp selbst bereit — entweder

    EXTRACTOR_AUTO_INSTALL=1   in der Umgebung setzen, oder
    ein eigenes Binary nach /usr/local/bin/yt-dlp mounten.

MSG
    fi
fi

exec "$@"
