# Privacy notes for operators · Datenschutzhinweise für Betreiber

*English first, Deutsch unten.*

Fundus has no telemetry and no analytics. It does not report to its authors.
What it stores and which connections it opens are listed here, so that whoever
runs an instance can write their own privacy notice. This page is not legal
advice.

## What is stored

| Where | What | How long |
|---|---|---|
| `data/app.db` · users | username, password hash (PBKDF2-SHA256, 600 000 rounds), API token for MCP, admin flag | until the account is deleted |
| `data/app.db` · users | last IP address (shown to admins, used for blocking) | overwritten on each request |
| `data/app.db` · users | platform cookies a user pasted in (Instagram, YouTube) | until the user removes them |
| `data/app.db` · events | user, type (download/transcript), time — no URLs, no titles | kept for the usage overview |
| memory · error log | the last 200 errors with URL and username; credentials are stripped | until restart or "clear" |
| `downloads/` | the files a user fetched | `CLEAN_AGE_HOURS` (default 6 h on servers, 7 days in the app) |

Known limitations, stated plainly:

- **Platform cookies and API tokens are stored unencrypted** in the database.
  Anyone with read access to `data/` can use them. Protect the volume and its
  backups accordingly. Users should paste cookies from a secondary account.
- The **macOS app** keeps everything under
  `~/Library/Application Support/Fundus/` on the user's own Mac.

## Outbound connections

| Destination | When | Off switch |
|---|---|---|
| pypi.org | version check for the installed components, at start and then once per `CHECK_INTERVAL_H` (default 24 h); sends only package names | `CHECK_INTERVAL_H=0` |
| pypi.org | installing updates — only when an admin presses the button, or `UPDATE_INTERVAL_H>0` | default off |
| the platform a user pasted a link for (YouTube, Instagram, TikTok, Threads, Spotify, Apple Podcasts, podcast feed hosts) | when that user asks for it | — |
| itunes.apple.com | podcast search and lookup | — |
| huggingface.co | downloading a Whisper model on first use | `WHISPER_ENABLED=0` |
| api.github.com (macOS app) | update check for the app itself, at most once a day after launch: asks for the newest release tag, sends no user data, installs nothing | — |
| pypi.org (macOS app) | installing yt-dlp and its JavaScript runtime, only after the user ticks the consent box under System → Media | nothing is installed without consent |

The macOS app installs no media extractor during setup. Installing one is a
separate step that happens only on the user's explicit consent.

Fonts are bundled and served by the instance itself; no font CDN is contacted.

---

## Deutsch

Fundus hat keine Telemetrie und keine Analyse. Es meldet nichts an die
Entwickler. Was es speichert und wohin es sich verbindet, steht hier, damit
Betreiber einer Instanz ihre eigene Datenschutzerklärung schreiben können.
Das ist keine Rechtsberatung.

### Was gespeichert wird

| Ort | Inhalt | Dauer |
|---|---|---|
| `data/app.db` · users | Benutzername, Passwort-Hash (PBKDF2-SHA256, 600 000 Runden), API-Token für MCP, Admin-Kennzeichen | bis das Konto gelöscht wird |
| `data/app.db` · users | letzte IP-Adresse (für Admins sichtbar, für Sperren genutzt) | wird bei jeder Anfrage überschrieben |
| `data/app.db` · users | Plattform-Cookies, die jemand eingefügt hat (Instagram, YouTube) | bis die Person sie entfernt |
| `data/app.db` · events | Nutzer, Art (Download/Transkript), Zeit — keine URLs, keine Titel | für die Nutzungsübersicht |
| Arbeitsspeicher · Fehlerprotokoll | die letzten 200 Fehler mit URL und Benutzername; Zugangsdaten werden entfernt | bis zum Neustart oder „Löschen“ |
| `downloads/` | die abgerufenen Dateien | `CLEAN_AGE_HOURS` (Server 6 h, App 7 Tage) |

Bekannte Grenzen, offen gesagt:

- **Plattform-Cookies und API-Tokens liegen unverschlüsselt** in der Datenbank.
  Wer `data/` lesen kann, kann sie benutzen. Volume und Backups entsprechend
  schützen. Cookies am besten von einem Zweitkonto einfügen.
- Die **Mac-App** speichert alles unter `~/Library/Application Support/Fundus/`
  auf dem eigenen Mac.

### Ausgehende Verbindungen

| Ziel | Wann | Abschalten |
|---|---|---|
| pypi.org | Versionsprüfung der installierten Komponenten, beim Start und dann alle `CHECK_INTERVAL_H` (Standard 24 h); übermittelt nur Paketnamen | `CHECK_INTERVAL_H=0` |
| pypi.org | Updates installieren — nur per Knopf oder bei `UPDATE_INTERVAL_H>0` | Standard aus |
| die Plattform des eingefügten Links (YouTube, Instagram, TikTok, Threads, Spotify, Apple Podcasts, Feed-Hoster) | wenn jemand es anfordert | — |
| itunes.apple.com | Podcast-Suche und -Auflösung | — |
| huggingface.co | Whisper-Modell beim ersten Einsatz laden | `WHISPER_ENABLED=0` |
| api.github.com (Mac-App) | Update-Prüfung der App selbst, höchstens einmal am Tag nach dem Start: fragt das neueste Release-Tag ab, sendet keine Nutzerdaten, installiert nichts | — |
| pypi.org (Mac-App) | yt-dlp und seine JavaScript-Laufzeit installieren, nur nachdem die Person unter System → Medien die Einwilligung angekreuzt hat | ohne Einwilligung wird nichts installiert |

Bei der Einrichtung installiert die Mac-App keinen Medien-Extraktor. Das ist
ein eigener Schritt und passiert nur mit ausdrücklicher Einwilligung.

Schriften liegen bei und werden von der Instanz selbst ausgeliefert; kein
Schriften-CDN wird kontaktiert.
