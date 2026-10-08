<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/wordmark-dark.svg">
    <img src="assets/brand/wordmark.svg" alt="Fundus" height="64">
  </picture>
</h1>

[English](README.md)

**Fundus macht aus Podcasts und Videos Material, mit dem deine KI arbeiten kann.**
Aus einer Aufnahme wird ein Transkript, aus einem Podcast-Link ein
durchsuchbares Folgenpaket, und beides holt sich Claude oder ein anderer
Assistent direkt über MCP. Fundus ist Open Source und du betreibst es selbst,
eine öffentliche Instanz gibt es nicht.

[![Lizenz: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Plattform](https://img.shields.io/badge/platform-Docker%20%7C%20macOS-lightgrey.svg)](#so-läuft-es)

---

Das meiste, was in Podcasts und Videos gesagt wird, wird nie zu Text. Du weißt
vielleicht noch, dass vor drei Wochen jemand etwas Kluges über Preise gesagt
hat, irgendwo in einer von zehn Folgen und irgendwann nach der ersten halben
Stunde. Um es wiederzufinden, hörst du halt alles noch mal.

KI-Assistenten sind gut darin, Texte zu vergleichen und daraus zu zitieren,
aber nur, wenn sie den Text auch haben. Bisher heißt das, Untertitel von Hand
zu kopieren oder einen Transkriptionsdienst pro Minute zu bezahlen, und oft
lässt man es dann ganz.

Mit Fundus fügst du einen Link ein, oder zehn, und bekommst saubere
Transkripte mit Zeitmarken. Fundus nimmt veröffentlichte Untertitel, wo es sie
gibt, das offizielle Transkript, wo ein Podcast eins anbietet, und Whisper auf
deiner eigenen Hardware, wo beides fehlt. Danach holt und durchsucht dein
Assistent die Texte selbst.

## Für wen

- Forschende, die mit Interviews und Vorlesungen arbeiten und zitierfähigen
  Text mit Zeitmarken brauchen.
- Journalistinnen und Journalisten, die vor dem Zitieren nachprüfen wollen,
  was genau gesagt wurde und an welcher Stelle.
- Podcast-Hörerinnen und -Hörer, die eine Sendung durchsuchen wollen, statt
  sich durch Stunden von Audio zu spulen.
- Alle, die mit KI-Assistenten bauen und Transkripte und Podcastfolgen lieber
  als MCP-Werkzeug hätten als per Copy-and-paste.

## Was es kann

**Transkripte.** Wo eine Plattform veröffentlichte Untertitel anbietet,
verwendet Fundus die. Sonst transkribiert es lokal mit Whisper (faster-whisper
auf Servern, MLX auf der Neural Engine von Macs mit Apple Silicon). Es gibt
klickbare Zeitmarken, eine Wortzahl und eine Suche im Text. Exportieren kannst
du als `txt`, `md`, `srt`, `vtt` oder `json`.

**Podcasts.** Du fügst Links von Spotify oder Apple Podcasts, aus einem
RSS-Feed oder von einer Website ein, und Fundus sucht zu jedem den passenden
Eintrag im Feed heraus, also Sendung, Titel, Datum, Dauer, Shownotes und, falls
die Sendung eins veröffentlicht, das offizielle Transkript. Zugeordnet wird
zuerst über die GUID, dann über Dauer und Datum und nur als letzter Ausweg über
den Titel, dann aber als unsicher markiert. Viele Sendungen veröffentlichen
gar kein Transkript. Für die schreibt Fundus ein fertiges
Whisper-Script (macOS mit MLX oder Windows-PowerShell), das die Folgen auf
deinem eigenen Rechner transkribiert. Eigennamen aus den Shownotes bekommt
Whisper mit, damit es sie richtig schreibt.

**Auftragszettel.** Wenn es um viele Folgen auf einmal geht, kann dir ein
Assistent einen Auftragszettel vorbereiten. Das ist eine kleine strukturierte
Liste im Format `fundus-podcast/1`, aus der Fundus ein Paket macht. Darin
stecken `manifest.json`, die Shownotes als Markdown, offizielle Transkripte, wo
es sie gibt, auf Wunsch der Ton in Sprachqualität und das Whisper-Script für
alles Übrige. Die Anleitung, die ein Assistent dafür braucht, liegt unter
`/api/podcast/auftragszettel.md`.

**Suche über Folgen hinweg.** In einem Podcast-Paket durchsuchst du alle
Transkripte auf einmal und bekommst Folge, Zeitmarke und Textstelle zurück,
also die Antwort auf „in welcher Folge ging es um X, und ab wann". Das geht
über MCP und über die API. Gesucht wird immer im jeweiligen Paket, einen Index
oder eine Historie früherer Läufe führt Fundus nicht.

**MCP-Server.** Unter `/mcp` gibt es einen Endpunkt, für den jeder Nutzer einen
persönlichen Bearer-Token hat. Darüber holt Claude oder ein anderer Assistent
Transkripte, löst Podcasts auf, baut Pakete und durchsucht sie, ohne dass du
dazwischen etwas kopieren musst. Details stehen [weiter unten](#mcp).

**Posts und Threads.** Textbeiträge und Threads speichert Fundus als Markdown,
zusammen mit den angehängten Bildern.

**Audio-Trimmer.** Im Browser siehst du eine Wellenform mit zwei Griffen,
kannst vorhören und bekommst einen sauberen Schnitt mit kurzer Blende. Damit
schneidest du zum Beispiel das Intro ab, ohne extra einen Audio-Editor zu
öffnen.

**Viele Links auf einmal.** Du fügst eine ganze Liste ein und siehst für jeden
Link, was gerade passiert. Duplikate markiert Fundus schon vorab.

**Zweisprachig.** Die Oberfläche gibt es auf Deutsch und Englisch. Sie richtet
sich nach deinem Browser und lässt sich jederzeit umschalten.

**Optional: Mediendateien abrufen.** Fundus kann auch die Mediendateien hinter
einem Link speichern (Videos, Fotos, Audio), wenn du die Inhalte kopieren
darfst. Das geht nur über Drittprogramme (yt-dlp, gallery-dl), die installiert,
wer die Instanz betreibt. Im veröffentlichten Container-Image sind sie nicht
enthalten, und in der Mac-App musst du es ausdrücklich einschalten. Mehr dazu
unter
[Optional: Extraktoren](#optional-extraktoren).

## Ehrliche Grenzen

- **Plattformen behandeln Rechenzentrums-IPs anders.** Manche Videoplattformen
  halten Anfragen von gemieteten Servern für verdächtig und geben dann keine
  Untertitel heraus, Anschlüsse von zu Hause gelten als normal. Daran kann
  keine Software etwas ändern. Wenn dich das betrifft, lass Fundus dort laufen,
  wo du bist, also als Mac-App oder auf einem Rechner mit privatem
  Internetanschluss. Oder du setzt auf offizielle Podcast-Transkripte und
  Whisper, die hängen nicht davon ab, ob eine Plattform Untertitel herausgibt.
- **Whisper auf einem kleinen Server ist langsam.** Das Modell `base` braucht
  auf vier Kernen etwa ein Zehntel der Laufzeit und ist bei Deutsch spürbar
  schwächer. Apple Silicon schafft auch das große Modell.
- **Die Podcast-Zuordnung ist nicht immer sicher.** Folgen, die nur über den
  Titel gefunden wurden, sind als `unsicher` markiert. Die solltest du vor dem
  Zitieren prüfen.
- **Dateien bleiben nicht lange liegen.** Ein Server löscht abgerufene Dateien
  nach `CLEAN_AGE_HOURS` (standardmäßig 6 Stunden, in der App 7 Tage). Was du
  behalten willst, speicherst du also besser selbst ab.

## So läuft es

Es gibt zwei Wege mit demselben Code. Du betreibst Fundus entweder als
Container auf einem eigenen Server oder als Mac-App, die komplett auf deinem
Mac läuft. Eine öffentliche Instanz gibt es nicht.

### Docker

```bash
git clone https://github.com/tillheidrich/fundus.git
cd fundus
cp .env.example .env      # anpassen, was du brauchst
docker compose up -d
```

Dann `http://localhost:8000` öffnen und das erste Konto anlegen. Es wird
automatisch Administrator, danach ist die Registrierung geschlossen, außer du
setzt `SIGNUP_CODE` (Einladungscode) oder `OPEN_SIGNUP=1`.

Ohne weitere Einstellungen kann der Container Podcasts, offizielle
Transkripte, Whisper und veröffentlichte Untertitel, für die kein Extraktor
nötig ist. Die Whisper-Modelle werden beim ersten Einsatz geladen und in einem
eigenen Volume aufbewahrt.

#### Optional: Extraktoren

Das Image enthält keinen Medien-Extraktor. Ob du einen hinzufügst, entscheidest
du als Betreiber, deshalb ist das standardmäßig aus. Entweder setzt du in der
`.env`

```bash
EXTRACTOR_AUTO_INSTALL=1
```

und yt-dlp und gallery-dl werden beim ersten Start installiert, oder du hängst
ein eigenes Binary unter `/usr/local/bin/yt-dlp` ein. Mediendateien von YouTube
bleiben trotzdem aus, bis du zusätzlich `ENABLE_YOUTUBE_VIDEO=1` setzt.

### macOS

Die Mac-App gibt es nicht zum Download, ein öffentliches `.dmg` existiert
nicht. Fertig gebaute Kopien gebe ich nur direkt an Familie, Freunde und
Forschungspartner weiter. Bauen kann sie aber jeder selbst aus dem Quellcode:

```bash
git clone https://github.com/tillheidrich/fundus.git
cd fundus/macos && ./build-app.sh
```

Dafür brauchst du die Xcode Command Line Tools (`xcode-select --install`), das
volle Xcode ist nicht nötig. Signieren ist optional: Ohne Signaturidentität
signiert das Skript die App ad hoc, und das reicht, um sie auf dem Mac zu
nutzen, auf dem sie gebaut wurde. Mit `./build-app.sh --install` landet sie
außerdem im Programme-Ordner.

Die App läuft komplett auf deinem Mac. Der Server lauscht nur auf der
Loopback-Schnittstelle, es gibt keinen Login und keine Telemetrie. Ausgehende
Verbindungen sind nur deine eigenen Anfragen und die Update-Prüfungen
(aufgelistet in [docs/PRIVACY.md](docs/PRIVACY.md)). Beim ersten Start richtet
die App unter `~/Library/Application Support/Fundus` alles ein, was sie
braucht, also eine Python-Laufzeit, ffmpeg und auf Apple Silicon MLX Whisper,
und zeigt dabei an, was sie gerade tut. Das dauert einmalig ein paar Minuten.
Ein per Homebrew installiertes ffmpeg nutzt sie so, wie es ist. Einen
Medien-Extraktor installiert sie bei der Einrichtung nicht.

Ohne deine ausdrückliche Einwilligung installiert die App keinen
Medien-Extraktor. Fundus selbst lädt keine Medien; unter System → Medien
kannst du das Open-Source-Werkzeug yt-dlp (samt der JavaScript-Laufzeit, die
es braucht) aus PyPI installieren lassen, nachdem du eine Einwilligung
angekreuzt hast. Das Laden von YouTube-Videos ist ab Werk aus und hat einen
eigenen Schalter mit eigenem Hinweis, ebenso die Download-Werkzeuge für
KI-Assistenten. Transkripte (Untertitel) von YouTube funktionieren immer.

Deine Daten liegen außerhalb des App-Bundles. Wenn du die App durch eine
neu gebaute Version ersetzt, bleiben Einstellungen und Dateien erhalten.

## Aktualisieren

**Docker mit dem veröffentlichten Image.** Die `docker-compose.yml` zeigt auf
`ghcr.io/tillheidrich/fundus:latest`, also reicht:

```bash
docker compose pull && docker compose up -d
```

**Docker aus dem Quellcode.**

```bash
git pull && docker compose up -d --build
```

**Mac-App aus dem Quellcode.** Neuen Stand holen und neu bauen:

```bash
git pull && cd macos && ./build-app.sh --install
```

Die eingebaute Update-Prüfung der App schaut zwar weiter nach neuen
Versionen, installieren kann sie aber nur Releases mit `.dmg`, und davon wird
keins veröffentlicht. Sie verweist dich deshalb nur auf die Release-Seite.
Aktualisieren heißt also: neu bauen.

**yt-dlp und die übrigen Komponenten.** Die aktualisierst du über den
Update-Knopf im Bereich System (auf einem Server auf der Kontoseite eines
Administrators). Automatische Updates sind standardmäßig aus,
`UPDATE_INTERVAL_H` schaltet sie ein. Einmal am Tag schaut Fundus bei PyPI nach
neuen Versionen, installiert wird dabei aber nichts.

## Konfiguration

Alle Einstellungen sind Umgebungsvariablen, und `.env.example` listet jede
davon mit Erklärung auf. Das hier sind die, die du am ehesten anfasst:

| Variable | Standard | |
|---|---|---|
| `SIGNUP_CODE` | leer | Einladungscode. Ohne ist die Registrierung nach dem ersten Konto zu |
| `OPEN_SIGNUP` | `0` | Jeder darf sich registrieren. Selten das, was du willst |
| `WHISPER_ENABLED` | `1` | Lokale Transkription, wenn es keine Untertitel gibt |
| `WHISPER_MODEL` | `base` | `tiny` · `base` · `small` · `medium` · `large-v3-turbo` |
| `WHISPER_MAX_MINUTES` | `45` | Längste Aufnahme, die der Server transkribiert |
| `PODCAST_MAX_EPISODES` | `25` | Folgen pro Podcast-Paket |
| `PODCAST_SERVER_WHISPER` | `0` | Podcast-Folgen auf dem Server transkribieren statt ein Script mitzugeben (gleichzeitige Läufe begrenzt durch `WHISPER_CONCURRENCY`, Standard `1`) |
| `CLEAN_AGE_HOURS` | `6` | Abgerufene Dateien werden danach gelöscht |
| `BATCH_MAX_URLS` | `150` | Links pro Durchlauf |
| `TRIM_MAX_MINUTES` | `180` | Längste Aufnahme, die der Trimmer annimmt |
| `EXTRACTOR_AUTO_INSTALL` | `0` | yt-dlp und gallery-dl beim ersten Start installieren |
| `ENABLE_YOUTUBE_VIDEO` | `0` | Mediendateien von YouTube erlauben (braucht einen Extraktor) |
| `ENABLE_MEDIA_TOOLS` | `0` | Die MCP-Tools `get_download_script` und `get_livestream_script` anbieten |
| `UPDATE_INTERVAL_H` | `0` | Stunden zwischen automatischen Komponenten-Updates. `0` schaltet ab |
| `CHECK_INTERVAL_H` | `24` | Stunden zwischen Versionsprüfungen. `0` schaltet ab |
| `TRUSTED_PROXIES` | leer | Adressen der Reverse-Proxys vor der Instanz |
| `APP_NAME` · `BRAND_TAG` | `Fundus` | Eigener Schriftzug, wenn du einen willst |
| `FUNDUS_LANG` | `en` | Sprache, wenn der Browser weder Deutsch noch Englisch verlangt |
| `TZ` | `UTC` | |

## Reverse-Proxy

Setz TLS vor die Instanz und sag Fundus, welchem Proxy es trauen darf, damit
es die echten Client-Adressen sieht:

```bash
TRUSTED_PROXIES=127.0.0.1
```

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 600s;   # Transkription ist keine schnelle Anfrage
}
```

Wenn `TRUSTED_PROXIES` gesetzt ist, fällt auch die Login-Ausnahme für den
Desktop-Betrieb weg. Eine Instanz hinter einem Proxy verlangt also immer einen
Login. Die Registrierung ist standardmäßig geschlossen, trotzdem sollte eine
Instanz im offenen Internet hinter einer Zugangskontrolle stehen, die du selbst
in der Hand hast.

## MCP

Fundus hat einen MCP-Server unter `/mcp` (Streamable HTTP) mit einem persönlichen Token pro Konto. Er funktioniert mit Claude, Codex und mit offenen Modellen über lokale Programme wie LM Studio, Open WebUI, Goose, Cline oder AnythingLLM. Die System-Seite der App (auf dem Server die Konto-Seite) zeigt für jeden dieser Assistenten den fertigen Eintrag mit deinem Token. Claude Desktop, Jan, Msty und oterm starten nur lokale Server; dort hilft die Brücke `mcp-remote` (braucht Node). Wie die Einträge genau aussehen, steht in der [englischen README](README.md#mcp). Die Clouds von Claude und ChatGPT erreichen keine lokale Adresse und bieten nur OAuth an, damit klappt es noch nicht.

| Werkzeug | |
|---|---|
| `get_transcript(url, lang, format)` | Transkript als `text`, `timestamps`, `segments`, `srt` oder `vtt` |
| `list_transcript_languages(url)` | Verfügbare Untertitelspuren |
| `get_caption(url)` | Der Beschreibungstext eines Beitrags |
| `get_metadata(urls[])` | Titel, Link, Urheber, Datum und Beschreibung mehrerer Links als Markdown |
| `resolve_podcast(items[])` | Podcast-Links zu Folgen, mit Zuordnungsqualität und offiziellen Transkript-URLs |
| `get_podcast_shownotes(url)` | Shownotes und Metadaten einer Folge als Markdown |
| `get_podcast_transcript(url, format)` | Das offizielle Transkript, wenn die Sendung eins veröffentlicht |
| `get_podcast_package(items[], audio)` | Ein Paket für viele Folgen oder einen Auftragszettel |
| `get_whisper_script(items[], os_kind)` | Ein lokales Whisper-Script für Folgen ohne Transkript |
| `search_podcast_transcripts(podcast_id, query)` | Welche Folge was gesagt hat, und ab welcher Zeitmarke |

Behandle den Token wie ein Passwort, denn wer ihn hat, handelt in deinem Namen.

## Datenschutz

Fundus hat keine Telemetrie und keine Analyse. Was es speichert, wie lange und
welche ausgehenden Verbindungen es aufbaut, steht in
[docs/PRIVACY.md](docs/PRIVACY.md), damit Betreiber ihre eigene
Datenschutzerklärung schreiben können. Wichtig ist dabei, dass Plattform-Cookies
und API-Tokens unverschlüsselt in der Datenbank liegen. Schütz das Daten-Volume
und seine Backups also entsprechend.

## Entwicklung

```bash
pip install -r requirements.txt
pytest                                 # Tests, ohne Netzwerk
uvicorn main:app --reload
tools/smoke.sh http://127.0.0.1:8000   # Ende-zu-Ende, geht ins Netz
```

Gebaut ist Fundus mit Python 3.10 oder neuer, FastAPI, Jinja2, faster-whisper
und dem MCP-Python-SDK, die Mac-Hülle ist in Swift geschrieben. In
[CONTRIBUTING.md](CONTRIBUTING.md) stehen Umfang, Konventionen und was nicht
gemergt wird. Probleme mit einem Extraktor gehören zu
[yt-dlp](https://github.com/yt-dlp/yt-dlp/issues).

Sicherheitslücken meldest du bitte an mail@tillheidrich.de und nicht in einem
öffentlichen Issue.

## Rechtliches

**Bestimmungsgemäßer Gebrauch.** Fundus ruft Transkripte, Untertitel,
Metadaten und Medien ab, auf die du zugreifen darfst: eigene Uploads, frei
lizenzierte Werke, Material, für das du eine Erlaubnis hast, oder Nutzungen,
die bei dir eine gesetzliche Schranke abdeckt (Zitat, Berichterstattung,
Forschung, Barrierefreiheit). Es ist kein Werkzeug, um Kopien von Werken zu
beschaffen, die du nicht kopieren darfst.

**Du bist verantwortlich.** Fundus ist Software, die du selbst betreibst; es
gibt keine öffentliche Instanz. Wer eine Instanz betreibt, entscheidet, was
sie abruft, und ist dafür verantwortlich, Urheberrecht, die
Nutzungsbedingungen der beteiligten Plattformen und gegenüber den eigenen
Nutzern das Datenschutzrecht einzuhalten.

**Technische Schutzmaßnahmen.** Fundus enthält keinen Code, der
Verschlüsselung oder DRM aushebelt, und wirbt nicht dafür, technische
Schutzmaßnahmen auszuhebeln, noch unterstützt es das. Mediendateien werden
über Drittprogramme (yt-dlp, gallery-dl) abgerufen. Das veröffentlichte
Container-Image enthält sie nicht; du installierst sie selbst, oder die
Mac-App installiert yt-dlp auf deinen Wunsch nach ausdrücklicher
Einwilligung. In manchen
Rechtsordnungen ist es eingeschränkt, Werkzeuge zu nutzen oder zu verbreiten,
die technische Maßnahmen aushebeln, etwa nach § 95a UrhG oder
17 U.S.C. § 1201 (DMCA). Deutsche Gerichte haben die URL-Signatur von YouTube
als wirksame technische Maßnahme eingestuft (OLG Hamburg, 5 U 54/23), und der
Bundesgerichtshof hat es im Oktober 2025 abgelehnt, sich mit der Revision zu
befassen. Die Mac-App installiert ohne deine ausdrückliche Einwilligung keinen
Extraktor, und das Laden von YouTube-Videos ist ab Werk aus. Prüf das Recht,
das für dich gilt.
**Dieser Abschnitt ist keine Rechtsberatung.**

**Keine Verbindung zu den Plattformen.** Fundus ist ein unabhängiges Projekt
und steht in keiner Verbindung zu YouTube/Google, Instagram/Threads/Meta,
TikTok/ByteDance, Spotify oder Apple, wird von ihnen weder unterstützt noch
gesponsert. Produktnamen gehören ihren Inhabern und werden nur verwendet, um
die Kompatibilität zu beschreiben. Pull Requests, die Unterstützung für
Dienste hinzufügen, die auf Rechtsverletzungen ausgerichtet sind, werden
abgelehnt.

**Gewährleistung.** Bereitgestellt „wie besehen", ohne Gewährleistung, wie in
den Abschnitten 15 und 16 der AGPL-3.0 geregelt.

## Lizenz

[AGPL-3.0-only](LICENSE). Komponenten Dritter und ihre Lizenzen stehen in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md); das Container-Image enthält
den ffmpeg-Build von Debian, dessen zugehöriger Quellcode bei Debian erhältlich
ist.

Fundus stammt von Till Heidrich.
