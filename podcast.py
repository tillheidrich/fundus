"""Podcast resolution: a link to an episode, and out comes the feed entry.

Podcasts are the easy case, and it is worth saying why: an RSS feed is
published precisely so that software fetches it. There is no signing scheme to
work around, no bot check, no cookie — podcast apps do exactly this, and the
enclosure URL in the feed is the file the publisher put there for downloading.
None of the care the video side needs applies here.

What is hard is the matching. A Spotify link carries no feed and no GUID, only
a title and a duration, and feed titles routinely carry suffixes that Spotify
strips ("… | Florian Baader, CEO meinGPT"). So the chain is: read the Spotify
page for a show name, find the show's feed through Apple's search, then
identify the episode inside the feed by GUID if we have one, by duration and
date if we do not, and by title only as a last resort — never silently.
"""
import asyncio
import ipaddress
import json
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree as ET

# Namespaces that matter. itunes carries duration and author; the podcast
# namespace carries the transcript link, which is the whole prize when a show
# publishes one.
NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "podcast": "https://podcastindex.org/namespace/1.0",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Apple answers roughly twenty times a minute before it starts refusing. One
# episode costs one or two calls, so a batch of ten is fine — but the show →
# feed mapping is worth keeping for a day. That is not user data and does not
# contradict the no-history rule.
_feed_cache: dict[str, tuple[float, str]] = {}
FEED_CACHE_TTL = 24 * 3600


# ── Outbound safety ───────────────────────────────────────────────────────────
# Every URL here comes from somewhere else: a form field, an order sheet, or —
# worse — a <link rel="alternate"> on a page the caller pointed us at. Without
# a check, "resolve this podcast" becomes "fetch this for me from inside the
# network", and the result lands in a zip the caller can download. That is a
# read primitive, not merely blind SSRF, and on a VPS with neighbours it
# reaches the metadata service.

_BLOCKED_NETS = (
    "0.", "10.", "127.", "169.254.",          # unspecified, private, loopback, link-local
    "192.168.", "100.64.",                    # private, carrier NAT
    *(f"172.{n}." for n in range(16, 32)),    # private /12
)


def _is_blocked_ip(ip: str) -> bool:
    """Darf diese Adresse angesprochen werden?

    Hier standen Textpräfixe, und die hatten drei Löcher. Das schwerste:
    `::ffff:127.0.0.1` ist der Loopback, passt aber auf keine der Regeln
    (`::1`, `::`, `fc`, `fd`, `fe80:`) — ein AAAA-Record unter
    Angreiferkontrolle genügte, um die Sperre zu umgehen. Dazu deckte
    "100.64." nur ein /16 statt des /10 ab, und 192.0.0.0/24, 198.18.0.0/15
    sowie 64:ff9b::/96 fehlten ganz.

    Die Bibliothek kennt diese Bereiche alle. Textvergleiche auf Adressen
    sind der Fehler — eine Adresse hat zu viele gültige Schreibweisen.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True                 # unlesbar heißt nicht erreichbar
    # IPv4-in-IPv6 zuerst auflösen, sonst wird ::ffff:127.0.0.1 als globale
    # IPv6-Adresse behandelt.
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    if addr.is_private or addr.is_loopback or addr.is_link_local \
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return True
    # Carrier-NAT (100.64/10) und die Benchmark-Netze gelten nicht als
    # „private", sind aber ebenso wenig Ziele für einen Feed-Abruf.
    extra = ("100.64.0.0/10", "192.0.0.0/24", "198.18.0.0/15", "64:ff9b::/96")
    return any(addr in ipaddress.ip_network(n) for n in extra
               if addr.version == ipaddress.ip_network(n).version)


def _resolve_checked(url: str) -> tuple[str, str]:
    """(url, ip) when every address of the host is allowed, else ("", "").
    The ip is the one the request must then actually go to."""
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return "", ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return "", ""
    try:
        import socket
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except Exception:
        return "", ""
    if not infos:
        return "", ""
    for info in infos:
        if _is_blocked_ip(info[4][0]):
            return "", ""
    return url, infos[0][4][0]


def safe_url(url: str) -> str:
    """Return the URL if it is safe to fetch, else "".

    http(s) only — urllib honours file:// and ftp://, and curl adds dict://,
    gopher:// and scp:// on top. Then every address the host resolves to is
    checked, because a name under the caller's control can point at 127.0.0.1
    just as easily as at a CDN.
    """
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    host = p.hostname
    try:
        import socket
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except Exception:
        return ""           # unresolvable is not fetchable either
    for info in infos:
        if _is_blocked_ip(info[4][0]):
            return ""
    return url


# ── HTTP ──────────────────────────────────────────────────────────────────────

FETCH_MAX_BYTES = 25 * 1024 * 1024     # a large feed is ~5 MB; anything far past that is not one


class _TooLarge(Exception):
    pass


async def fetch(url: str, timeout: int = 20, as_json: bool = False,
                max_bytes: int = FETCH_MAX_BYTES):
    """One place for outbound requests, so the user agent and timeout are not
    scattered. curl_cffi where available — Spotify is terse with plain urllib.

    Redirects are followed by hand, one hop at a time, because an allowed host
    is free to redirect to 127.0.0.1 and a library that follows automatically
    would take it there without asking.
    """
    def _get(target: str, ip: str):
        # The address checked is the address used. Without pinning, curl
        # resolves the name a second time, and a name with a zero TTL can
        # answer with a public address for the check and 169.254.169.254
        # for the request (DNS rebinding).
        pu = urlparse(target)
        port = pu.port or (443 if pu.scheme == "https" else 80)
        pin = f"{pu.hostname}:{port}:{('[' + ip + ']') if ':' in ip else ip}"
        try:
            from curl_cffi import requests as cffi
            from curl_cffi import CurlOpt
            got = bytearray()

            def collect(chunk):
                got.extend(chunk)
                if len(got) > max_bytes:
                    raise _TooLarge()
                return len(chunk)

            try:
                sess = cffi.Session(curl_options={CurlOpt.RESOLVE: [pin]})
            except TypeError:           # very old curl_cffi: refuse rather than fetch unpinned
                return 0, b"", ""
            with sess:
                r = sess.get(target, timeout=timeout, impersonate="chrome",
                             headers={"User-Agent": UA}, allow_redirects=False,
                             content_callback=collect)
            return r.status_code, bytes(got), r.headers.get("location", "")
        except _TooLarge:
            return 0, b"", ""
        except ImportError:
            import urllib.request
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **kw):
                    return None
            opener = urllib.request.build_opener(_NoRedirect)
            req = urllib.request.Request(target, headers={"User-Agent": UA})
            try:
                with opener.open(req, timeout=timeout) as resp:
                    body = resp.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        return 0, b"", ""
                    return resp.status, body, ""
            except urllib.error.HTTPError as e:
                return e.code, b"", e.headers.get("Location", "")

    current, ip = _resolve_checked(url)
    if not current:
        return None
    code = body = None
    for _ in range(4):
        try:
            code, body, location = await asyncio.to_thread(_get, current, ip)
        except Exception:
            return None
        if code not in (301, 302, 303, 307, 308) or not location:
            break
        current, ip = _resolve_checked(urljoin(current, location))
        if not current:
            return None
    else:
        return None             # too many hops
    if code != 200:
        return None
    if as_json:
        try:
            return json.loads(body)
        except Exception:
            return None
    return body


async def head_is_audio(url: str, timeout: int = 12) -> bool:
    """Confirm an enclosure actually serves audio before promising it does.
    Some feeds point at a landing page."""
    current = safe_url(url)
    if not current:
        return False

    def _head():
        """Weiterleitungen von Hand, Sprung für Sprung.

        Hier stand `allow_redirects=True`, und urllib folgt ohnehin von
        selbst. Geprüft wurde also nur die erste Adresse: ein erlaubter
        Feed-Host durfte anschließend auf 127.0.0.1 oder den
        Metadaten-Dienst umleiten, und die Prüfung lief mit. `fetch()` weiter
        oben macht es seit jeher richtig — hier fehlte es.
        """
        target = current
        for _ in range(4):
            try:
                from curl_cffi import requests as cffi
                r = cffi.head(target, timeout=timeout, impersonate="chrome",
                              headers={"User-Agent": UA}, allow_redirects=False)
                code, loc = r.status_code, r.headers.get("location", "")
                ctype = r.headers.get("content-type", "")
            except ImportError:
                import urllib.request
                class _NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *a, **kw):
                        return None
                opener = urllib.request.build_opener(_NoRedirect)
                req = urllib.request.Request(target, method="HEAD",
                                             headers={"User-Agent": UA})
                try:
                    with opener.open(req, timeout=timeout) as resp:
                        code, loc, ctype = resp.status, "", resp.headers.get("Content-Type", "")
                except urllib.error.HTTPError as e:
                    code, loc, ctype = e.code, e.headers.get("Location", ""), ""
            if code not in (301, 302, 303, 307, 308) or not loc:
                return ctype
            target = safe_url(urljoin(target, loc))
            if not target:
                return ""
        return ""
    try:
        ct = (await asyncio.to_thread(_head) or "").lower()
    except Exception:
        return False
    # m4a shows up as audio/x-m4a, audio/mp4 and occasionally video/mp4.
    return ct.startswith("audio/") or "mp4" in ct or "mpeg" in ct


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_duration(raw: str | None) -> int:
    """itunes:duration is 'HH:MM:SS', 'MM:SS' or plain seconds, depending on
    who generated the feed."""
    if not raw:
        return 0
    raw = str(raw).strip()
    if raw.isdigit():
        return int(raw)
    parts = [p for p in raw.split(":") if p != ""]
    try:
        nums = [float(p.replace(",", ".")) for p in parts]
    except ValueError:
        return 0
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60 + n
    return int(seconds)


def parse_date(raw: str | None) -> datetime | None:
    """Ein Datum aus einem Feed oder einem Auftragszettel.

    Gibt immer ein Datum MIT Zeitzone zurück, oder None. Das ist der ganze
    Vertrag dieser Funktion, und er wurde an einer Stelle gebrochen:
    `parsedate_to_datetime` liefert ein Datum ohne Zeitzone, wenn der Feed
    keine nennt (ein RFC-822-Datum darf den Offset weglassen, und `-0000`
    bedeutet ausdrücklich „unbekannt"). Der Zweig darunter normalisierte
    sorgfältig auf UTC, dieser gab durch, was er bekam.

    Die Folge war kein falsches Datum, sondern ein Abbruch: sobald ein Feed
    ein Datum ohne Offset führte und das gesuchte eines mit hatte, scheiterte
    der Abgleich mit »can't subtract offset-naive and offset-aware
    datetimes« — eine Meldung, die wie ein Fehler im Auftragszettel aussieht
    und keiner war.

    UTC als Annahme für ein Datum ohne Offset ist nicht korrekt, sondern die
    unschädlichste Wahl: der Abgleich erlaubt zwei Tage Abweichung, und
    danach ist jede Zeitzone der Welt innerhalb der Toleranz.
    """
    if not raw:
        return None

    def aware(d: datetime | None) -> datetime | None:
        if d is None:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    try:
        return aware(parsedate_to_datetime(raw))
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return aware(datetime.strptime(raw.strip(), fmt))
        except ValueError:
            continue
    return None


def normalise_title(t: str) -> str:
    """Strip what the platforms disagree about before comparing.

    Feed titles carry guest names and show suffixes after a pipe or dash that
    Spotify does not show; episode numbering appears in one and not the other.
    """
    t = (t or "").lower()
    t = re.split(r"\s*[|•·]\s*", t)[0]
    t = re.sub(r"^\s*(folge|episode|ep\.?|#)\s*\d+\s*[:\-–—]?\s*", "", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalise_title(a), normalise_title(b)).ratio()


def strip_html(html: str) -> str:
    """Show notes arrive as HTML. Links are the valuable part — many shows put
    their sources there — so they survive as Markdown."""
    if not html:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.I)
    s = re.sub(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
               lambda m: f"[{re.sub(r'<[^>]+>', '', m.group(2)).strip()}]({m.group(1)})",
               s, flags=re.I | re.S)
    s = re.sub(r"<li\b[^>]*>", "- ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    import html as _html
    s = _html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# ── Feed ──────────────────────────────────────────────────────────────────────

def parse_feed(xml_bytes: bytes) -> dict:
    """Read a podcast feed into show metadata plus a list of episodes."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return {}
    channel = root.find("channel")
    if channel is None:
        return {}

    def text(el, path, ns=None):
        found = el.find(path, ns) if ns else el.find(path)
        return (found.text or "").strip() if found is not None and found.text else ""

    show = {
        "title": text(channel, "title"),
        "author": text(channel, "itunes:author", NS) or text(channel, "managingEditor"),
        "description": strip_html(text(channel, "description")),
        "language": text(channel, "language") or "de",
        "link": text(channel, "link"),
    }

    episodes = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        audio = enclosure.get("url", "") if enclosure is not None else ""

        # podcast:transcript is the whole reason to look: a published
        # transcript beats anything Whisper produces, and it is free.
        transcripts = []
        for tr in item.findall("podcast:transcript", NS):
            url = tr.get("url", "")
            if url:
                transcripts.append({
                    "url": url,
                    "type": (tr.get("type", "") or "").split("/")[-1] or "unknown",
                    "language": tr.get("language", ""),
                })

        guid_el = item.find("guid")
        episodes.append({
            "guid": (guid_el.text or "").strip() if guid_el is not None and guid_el.text else "",
            "title": text(item, "title"),
            "date": text(item, "pubDate"),
            "duration_s": parse_duration(text(item, "itunes:duration", NS)),
            "audio": audio,
            "audio_type": (enclosure.get("type", "") if enclosure is not None else ""),
            "audio_bytes": int(enclosure.get("length", 0) or 0) if enclosure is not None else 0,
            "shownotes": strip_html(
                text(item, "content:encoded", NS) or text(item, "description")),
            "link": text(item, "link"),
            "episode_no": text(item, "itunes:episode", NS),
            "season": text(item, "itunes:season", NS),
            "transcripts": transcripts,
        })
    return {"show": show, "episodes": episodes}


# ── Resolution chain ──────────────────────────────────────────────────────────

SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?episode/([A-Za-z0-9]+)")
APPLE_RE = re.compile(r"podcasts\.apple\.com/.*?/podcast/.*?/id(\d+)")


async def from_spotify(url: str) -> dict:
    """Read title, show and duration for a Spotify episode.

    Three sources, in this order, because the obvious one stopped working:

    1. The embed page. It ships its data as JSON in `__NEXT_DATA__`, where the
       show name sits in `subtitle` — no prose to split on a middle dot, and
       the duration arrives as a number instead of a string.
    2. The episode page's og: tags. This used to be the only path; as of
       10/2026 open.spotify.com answers a non-browser client with a 930-byte
       JS shell titled "Spotify – Web Player" and not one og: tag in it. Kept
       as a fallback, because a shell is cheap to detect and Spotify has
       reverted such things before.
    3. oEmbed. Title only, no show — not enough to find a feed, but enough to
       tell "exclusive or deleted" apart from "we could not read the page",
       and those two deserve different messages.
    """
    m = SPOTIFY_RE.search(url)
    ep_id = m.group(1) if m else ""

    # ── 1. embed page ────────────────────────────────────────────────────────
    if ep_id:
        body = await fetch(f"https://open.spotify.com/embed/episode/{ep_id}")
        if body:
            blob = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                body.decode("utf-8", errors="replace"), re.S)
            if blob:
                try:
                    data = json.loads(blob.group(1))
                    ent = (data.get("props", {}).get("pageProps", {})
                               .get("state", {}).get("data", {}).get("entity") or {})
                except Exception:
                    ent = {}
                title = (ent.get("name") or ent.get("title") or "").strip()
                if title:
                    iso = (ent.get("releaseDate") or {}).get("isoString") or ""
                    return {
                        "title": title,
                        "show": (ent.get("subtitle") or "").strip(),
                        # Spotify counts milliseconds; the feed matcher seconds.
                        "duration_s": int((ent.get("duration") or 0) // 1000),
                        "date": iso[:10],
                        "description": ent.get("description") or "",
                        "source": url,
                    }

    # ── 2. og: tags on the episode page ─────────────────────────────────────
    body = await fetch(url)
    if body:
        html = body.decode("utf-8", errors="replace")

        def meta(prop: str) -> str:
            m = (re.search(rf'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']*)["\']', html, re.I)
                 or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{prop}["\']', html, re.I)
                 or re.search(rf'<meta[^>]+name=["\']{prop}["\'][^>]+content=["\']([^"\']*)["\']', html, re.I))
            import html as _h
            return _h.unescape(m.group(1).strip()) if m else ""

        title = meta("og:title")
        if title:
            desc = meta("og:description")
            # "Show name · Episode" — the show is what we search Apple for.
            show = desc.split("·")[0].strip() if "·" in desc else ""
            return {
                "title": title,
                "show": show,
                "duration_s": int(meta("music:duration") or 0),
                "date": meta("music:release_date"),
                "description": desc,
                "source": url,
            }

    # ── 3. oEmbed ───────────────────────────────────────────────────────────
    if ep_id:
        data = await fetch(
            "https://open.spotify.com/oembed?url="
            f"{quote(f'https://open.spotify.com/episode/{ep_id}', safe='')}",
            as_json=True)
        title = ((data or {}).get("title") or "").strip()
        if title:
            return {"title": title, "show": "", "duration_s": 0, "date": "",
                    "description": "", "source": url,
                    "note": "Nur der Titel war von Spotify zu bekommen, kein Sendungsname."}

    return {}


def feed_from_hints(feeds: dict | None, show: str) -> str:
    """Einen Feed aus dem mitgebrachten Verzeichnis holen.

    Normalisiert beim Vergleich, weil der Sendungsname aus drei Quellen
    kommen kann und selten dreimal gleich geschrieben ist: Spotify schneidet
    Zusätze ab, Feeds führen sie, und wer den Zettel von Hand tippt, lässt
    Satzzeichen weg.
    """
    if not feeds or not show:
        return ""
    want = normalise_title(show)
    for name, url in feeds.items():
        if not isinstance(url, str) or not url.strip():
            continue
        if normalise_title(str(name)) == want:
            return safe_url(url.strip())
    return ""


def order_sheet_feeds(text: str) -> dict:
    """Das Feed-Verzeichnis aus einem Auftragszettel oder einem Manifest.

    Zwei Quellen, und die zweite ist die eigentliche Bequemlichkeit: man kann
    ein `manifest.json` aus einem früheren Lauf unverändert einwerfen. Dort
    steht bei jeder Folge schon `show` und `feed`, also ist das Verzeichnis
    darin enthalten, ohne dass jemand etwas vorbereiten muss. Fundus schreibt
    zusätzlich eine `feeds`-Tabelle ins Manifest, damit auch Sendungen
    erhalten bleiben, von denen man später keine Folge mehr im Zettel hat.
    """
    text = (text or "").strip()
    if not text or text[0] not in "{[":
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}

    out: dict = {}
    if isinstance(data, dict):
        for name, url in (data.get("feeds") or {}).items():
            if isinstance(url, str) and url.strip():
                out[str(name)] = url.strip()
    episodes = data.get("episodes", []) if isinstance(data, dict) else data
    for ep in episodes if isinstance(episodes, list) else []:
        if not isinstance(ep, dict):
            continue
        show, feed = (ep.get("show") or "").strip(), (ep.get("feed") or "").strip()
        # Nicht überschreiben: eine ausdrückliche `feeds`-Angabe ist die
        # bewusstere, eine aus den Folgen abgeleitete nur ein Nebenprodukt.
        if show and feed and show not in out:
            out[show] = feed
    return out


async def apple_find_feed(show_name: str) -> str:
    """Find a show's feed through Apple's public search. No key, no account."""
    if not show_name:
        return ""
    key = show_name.lower().strip()
    hit = _feed_cache.get(key)
    if hit and time.time() - hit[0] < FEED_CACHE_TTL:
        return hit[1]

    data = await fetch(
        "https://itunes.apple.com/search?media=podcast&entity=podcast"
        f"&country=de&limit=5&term={quote(show_name)}", as_json=True)
    results = (data or {}).get("results") or []
    if not results:
        return ""
    # Apple's first hit is usually right, but check the name before trusting it.
    best, score = "", 0.0
    for r in results:
        s = title_similarity(show_name, r.get("collectionName", ""))
        if s > score:
            best, score = r.get("feedUrl", ""), s
    if best and score >= 0.6:
        _feed_cache[key] = (time.time(), best)
        return best
    return results[0].get("feedUrl", "") if results else ""


async def apple_feed_by_id(collection_id: str) -> str:
    data = await fetch(f"https://itunes.apple.com/lookup?id={collection_id}&country=de",
                       as_json=True)
    results = (data or {}).get("results") or []
    return results[0].get("feedUrl", "") if results else ""


async def feed_from_website(url: str) -> str:
    """Last automatic resort: a show's own page usually advertises its feed."""
    body = await fetch(url)
    if not body:
        return ""
    html = body.decode("utf-8", errors="replace")
    m = re.search(r'<link[^>]+type=["\']application/rss\+xml["\'][^>]+href=["\']([^"\']+)["\']',
                  html, re.I) or re.search(
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/rss\+xml["\']', html, re.I)
    if m:
        return m.group(1)
    m = re.search(r'https?://[^\s"\'<>]+(?:/rss|/feed|/podcast/rss)[^\s"\'<>]*', html, re.I)
    return m.group(0) if m else ""


# ── Matching ──────────────────────────────────────────────────────────────────

def match_episode(wanted: dict, episodes: list[dict]) -> tuple[dict | None, str, str]:
    """Identify an episode in a feed. Returns (episode, how, note).

    Order is deliberate and the reason is in the data: feed titles carry
    suffixes Spotify strips, so title matching is unreliable and comes last.
    Duration plus date is near-certain; a GUID is certain.

    Never guesses silently — an ambiguous result comes back as 'unsicher' with
    the best candidate attached, for a human to confirm.
    """
    if not episodes:
        return None, "", "Feed enthält keine Folgen."

    guid = (wanted.get("guid") or "").strip()
    if guid:
        for e in episodes:
            if e["guid"] and e["guid"] == guid:
                return e, "guid", ""

    want_dur = int(wanted.get("duration_s") or 0)
    want_date = parse_date(wanted.get("date"))
    if want_dur and want_date:
        hits = []
        for e in episodes:
            ed = parse_date(e.get("date"))
            if not ed or not e.get("duration_s"):
                continue
            if abs(e["duration_s"] - want_dur) <= 5 and abs((ed - want_date).days) <= 2:
                hits.append(e)
        if len(hits) == 1:
            return hits[0], "dauer+datum", ""
        if len(hits) > 1:
            return hits[0], "unsicher", f"{len(hits)} Folgen passen auf Dauer und Datum."

    want_title = wanted.get("title") or ""
    if want_title:
        scored = sorted(((title_similarity(want_title, e["title"]), e) for e in episodes),
                        key=lambda x: -x[0])
        best_score, best = scored[0]
        if best_score >= 0.85:
            runner = scored[1][0] if len(scored) > 1 else 0.0
            if best_score - runner < 0.05:
                return best, "unsicher", f"Zwei Folgen ähneln sich stark ({best_score:.2f}/{runner:.2f})."
            return best, "titel", ""
        return best, "unsicher", f"Bester Titeltreffer nur {best_score:.2f}."

    return None, "", "Zu wenig Information zum Abgleich."


# ── The chain, end to end ─────────────────────────────────────────────────────

async def resolve(item: str | dict, feeds: dict | None = None) -> dict:
    """Turn a link or an order-sheet entry into a resolved episode.

    `feeds` ist ein Verzeichnis Sendung → Feed, das der Aufrufer mitbringt —
    aus einem früheren Manifest oder aus dem Auftragszettel. Fundus merkt es
    sich bewusst nicht selbst: das Versprechen „keine Historie" soll gelten,
    und eine Tabelle auf der Platte wäre ein Verzeichnis der Sendungen, die
    jemand aufgelöst hat. In den eigenen Dateien ist dasselbe Wissen besser
    aufgehoben — es reist mit dem Auftragszettel und lässt sich an einen
    Assistenten weitergeben.
    """
    wanted: dict = {"source": item} if isinstance(item, str) else dict(item)
    url = (wanted.get("source") or "").strip() if isinstance(wanted.get("source"), str) else ""
    feed_url = (wanted.get("feed") or "").strip()
    notes: list[str] = []

    if url and SPOTIFY_RE.search(url):
        meta = await from_spotify(url)
        if not meta.get("title"):
            return {**wanted, "status": "nicht_aufgeloest",
                    "note": "Spotify-Seite nicht lesbar. Exklusivfolge oder entfernt?"}
        wanted = {**meta, **{k: v for k, v in wanted.items() if v}}
        # Erst das mitgebrachte Verzeichnis, dann Apple. Die Apple-Suche ist
        # die unzuverlässigste Stufe der Kette: sie hängt am Namen, und der
        # steht dort manchmal anders als bei Spotify. Wer die Sendung schon
        # einmal aufgelöst hat, hat den Feed in seinem Manifest — damit ist
        # der Schritt übersprungen, nicht bloß beschleunigt.
        if not feed_url and meta.get("show"):
            feed_url = feed_from_hints(feeds, meta["show"])
        if not feed_url and meta.get("show"):
            feed_url = await apple_find_feed(meta["show"])
            if not feed_url:
                notes.append(f"Kein Feed für „{meta['show']}“ gefunden.")

    elif url and APPLE_RE.search(url):
        feed_url = feed_url or await apple_feed_by_id(APPLE_RE.search(url).group(1))

    elif url and not feed_url:
        # A direct feed link, or a show's website.
        body = await fetch(url)
        if body and body.lstrip()[:5].lower().startswith((b"<?xml", b"<rss")):
            feed_url = url
        else:
            feed_url = await feed_from_website(url)

    if not feed_url:
        return {**wanted, "status": "nicht_aufgeloest",
                "note": "; ".join(notes) or "Kein Feed gefunden."}

    raw = await fetch(feed_url, timeout=25)
    if not raw:
        return {**wanted, "feed": feed_url, "status": "nicht_aufgeloest",
                "note": "Feed nicht abrufbar."}
    parsed = parse_feed(raw)
    if not parsed:
        return {**wanted, "feed": feed_url, "status": "nicht_aufgeloest",
                "note": "Feed nicht lesbar."}

    episode, how, note = match_episode(wanted, parsed["episodes"])
    if not episode:
        return {**wanted, "feed": feed_url, "status": "nicht_aufgeloest",
                "note": note, "show": parsed["show"].get("title", "")}

    return {
        "status": "unsicher" if how == "unsicher" else "ok",
        "match": how,
        "note": note,
        "source": url,
        "feed": feed_url,
        "show": parsed["show"].get("title", "") or wanted.get("show", ""),
        "author": parsed["show"].get("author", ""),
        "language": wanted.get("language") or parsed["show"].get("language", "de"),
        "guid": episode["guid"],
        "title": episode["title"],
        "date": episode["date"],
        "duration_s": episode["duration_s"],
        "audio": episode["audio"],
        "audio_type": episode["audio_type"],
        "audio_bytes": episode["audio_bytes"],
        "shownotes": episode["shownotes"],
        "link": episode["link"] or url,
        "transcripts": episode["transcripts"],
        "hints": wanted.get("hints", ""),
    }


async def resolve_many(items: list, concurrency: int = 4,
                       feeds: dict | None = None) -> list[dict]:
    """Resolve a batch. Bounded concurrency: Apple starts refusing at roughly
    twenty calls a minute, and four at a time stays well clear."""
    sem = asyncio.Semaphore(concurrency)

    async def one(it):
        async with sem:
            try:
                return await resolve(it, feeds=feeds)
            except Exception as e:
                src = it if isinstance(it, str) else it.get("source", "")
                return {"source": src, "status": "nicht_aufgeloest",
                        "note": f"Fehler: {str(e)[:150]}"}

    return await asyncio.gather(*(one(i) for i in items))


# ── Order sheet ───────────────────────────────────────────────────────────────

SCHEMA = "fundus-podcast/1"


def parse_order_sheet(text: str) -> list:
    """Accept what a person or an assistant is likely to paste: JSON in the
    documented schema, or simply one link per line."""
    text = (text or "").strip()
    if not text:
        return []
    if text[0] in "{[":
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data.get("episodes", []) or []
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            m = re.search(r"https?://\S+", line)
            if m:
                out.append(m.group(0))
    return out


def episode_slug(ep: dict) -> str:
    """<Show-short>_<date>_<title-short>, as the concept fixed it."""
    def short(s: str, n: int) -> str:
        s = re.sub(r"[^\w\s-]", "", s or "", flags=re.UNICODE).strip()
        s = re.sub(r"\s+", "-", s)
        return s[:n].strip("-") or "Folge"

    d = parse_date(ep.get("date"))
    date = d.strftime("%Y-%m-%d") if d else "ohne-datum"
    return f"{short(ep.get('show', ''), 20)}_{date}_{short(ep.get('title', ''), 40)}"


def shownotes_markdown(ep: dict) -> str:
    """Front matter for machines, show notes below for people.

    The notes are often the most valuable part for research — many shows put
    their sources there — so they are kept whole rather than summarised.
    """
    def esc(v) -> str:
        s = str(v or "")
        return '"' + s.replace('"', '\\"') + '"' if any(c in s for c in ':#"\n') else s

    d = parse_date(ep.get("date"))
    lines = [
        "---",
        f"titel: {esc(ep.get('title'))}",
        f"sendung: {esc(ep.get('show'))}",
        f"autor: {esc(ep.get('author'))}",
        f"datum: {d.strftime('%Y-%m-%d') if d else ''}",
        f"dauer: {ep.get('duration_s', 0)}",
        f"episoden_link: {esc(ep.get('link'))}",
        f"spotify: {esc(ep.get('source') if 'spotify' in (ep.get('source') or '') else '')}",
        f"feed: {esc(ep.get('feed'))}",
        f"guid: {esc(ep.get('guid'))}",
        f"audio: {esc(ep.get('audio'))}",
        f"sprache: {esc(ep.get('language', 'de'))}",
        f"transkript_quelle: {'offiziell' if ep.get('transcripts') else 'whisper'}",
    ]
    if ep.get("transcripts"):
        lines.append("transkript_urls:")
        for t in ep["transcripts"]:
            lines.append(f"  - {t['url']}")
    lines += [
        f"abgerufen: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
        "---",
        "",
        f"# {ep.get('title', '')}",
        "",
        f"**{ep.get('show', '')}**"
        + (f" · {d.strftime('%d.%m.%Y')}" if d else "")
        + (f" · {ep.get('duration_s', 0) // 60} Min" if ep.get("duration_s") else ""),
        "",
        ep.get("shownotes") or "_Keine Shownotes im Feed._",
    ]
    return "\n".join(lines) + "\n"


def build_manifest(episodes: list[dict], options: dict | None = None) -> dict:
    out = []
    for ep in episodes:
        entry = {k: ep.get(k) for k in (
            "status", "match", "note", "source", "feed", "guid", "title", "show",
            "date", "duration_s", "audio", "language", "hints")}
        entry["transcripts"] = ep.get("transcripts", [])
        if ep.get("files"):
            entry["files"] = ep["files"]
        out.append({k: v for k, v in entry.items() if v not in (None, "", [])})
    # Das Feed-Verzeichnis dieses Laufs, ausdrücklich und nicht nur nebenbei
    # in den Folgen. Damit lässt sich dieses Manifest später unverändert als
    # Auftragszettel einwerfen und ersetzt die Apple-Suche — die
    # unzuverlässigste Stufe der Kette. Fundus selbst merkt sich nichts; das
    # Wissen liegt in dieser Datei, also beim Nutzer.
    feeds = {}
    for ep in episodes:
        show, feed = (ep.get("show") or "").strip(), (ep.get("feed") or "").strip()
        if show and feed:
            feeds.setdefault(show, feed)
    return {"schema": SCHEMA,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "options": options or {},
            "feeds": feeds,
            "episodes": out}


# ── The brief for an assistant ────────────────────────────────────────────────
#
# Fields an order sheet may carry, each with the line in this file that reads
# it. The pairing is not decoration: a test walks this table and fails if a
# field here is no longer consumed anywhere, which is what keeps the brief from
# describing a schema the code has moved past.
ORDER_SHEET_FIELDS: dict[str, str] = {
    "source": "Link zur Folge — Spotify, Apple, RSS oder die Seite der Sendung. "
              "Ohne alles andere reicht er meistens; mit den Feldern darunter "
              "wird daraus eine Zuordnung statt einer Schätzung.",
    "feed": "Adresse des RSS-Feeds. Spart die Apple-Suche und damit die "
            "wahrscheinlichste Fehlerquelle: eine Sendung, deren Name bei "
            "Apple anders geschrieben steht.",
    "guid": "Die GUID aus dem Feed. Das einzige Feld, das die Folge sicher "
            "identifiziert — damit hört das Raten auf.",
    "duration_s": "Länge in SEKUNDEN, ganzzahlig. Zusammen mit dem Datum so "
                  "gut wie eindeutig. Vorsicht bei Spotify: dort steht sie in "
                  "Millisekunden.",
    "date": "Veröffentlichungsdatum. ISO (2026-10-05) oder RFC 822 aus dem "
            "Feed, beides wird gelesen.",
    "title": "Folgentitel. Nur die dritte Wahl beim Abgleich, weil Feed-Titel "
             "Zusätze führen, die Spotify abschneidet.",
    "show": "Name der Sendung. Wird gebraucht, wenn kein Feed dabeisteht.",
    "language": "Sprachkürzel, etwa de oder en. Steuert Whisper.",
    "hints": "Eigennamen, die Whisper sonst verhört — Gäste, Firmen, "
             "Fachbegriffe. Höchstens eine Handvoll, der Prompt ist auf 200 "
             "Zeichen begrenzt und läuft sonst über.",
}


def order_sheet_brief(base_url: str = "") -> str:
    """The text you hand an assistant so that what comes back fits.

    Generated, not written by hand, and generated from the same constants the
    parser uses: SCHEMA and ORDER_SHEET_FIELDS. A brief kept in a separate
    file is correct on the day it is written and wrong two commits later, and
    nobody notices, because nothing reads it except the assistant — which will
    cheerfully produce the obsolete shape.
    """
    lines = [
        f"# Auftragszettel für Fundus — Schema `{SCHEMA}`",
        "",
        "Du sollst Podcast-Folgen für Fundus vorbereiten. Fundus lädt Ton und",
        "Shownotes selbst und erzeugt bei Bedarf ein Whisper-Script; was du",
        "lieferst, ist die Identifikation der Folgen.",
        "",
        "## Was du zurückgibst",
        "",
        "Ein einziges JSON-Objekt, nichts davor und nichts danach — kein",
        "Markdown-Zaun, keine Erklärung. Fundus nimmt auch eine reine",
        "Linkliste (ein Link pro Zeile), aber dann muss es die Folge im Feed",
        "über Titel und Dauer suchen, und das ist eine Schätzung.",
        "",
        "```json",
        "{",
        f'  "schema": "{SCHEMA}",',
        '  "episodes": [',
        "    {",
        '      "source": "https://open.spotify.com/episode/…",',
        '      "feed": "https://feeds.example.com/show.xml",',
        '      "guid": "a3f1c0de-2026-10-05",',
        '      "duration_s": 2352,',
        '      "date": "2026-10-05",',
        '      "title": "Was passiert, wenn KI-Agenten gehackt werden?",',
        '      "show": "Beispiel-Podcast",',
        '      "language": "de",',
        '      "hints": "Viviana Sutedjo, METR"',
        "    }",
        "  ]",
        "}",
        "```",
        "",
        "## Die Felder",
        "",
        "`source` ist das Minimum. Jedes weitere Feld macht die Zuordnung",
        "sicherer; keines ist Pflicht.",
        "",
        "| Feld | Bedeutung |",
        "|------|-----------|",
    ]
    for name, meaning in ORDER_SHEET_FIELDS.items():
        lines.append(f"| `{name}` | {meaning} |")
    lines += [
        "",
        "## Feeds von früher mitgeben",
        "",
        "Oben neben `episodes` darf eine Tabelle `feeds` stehen, Sendungsname",
        "auf Feed-Adresse:",
        "",
        "```json",
        '{ "schema": "' + SCHEMA + '",',
        '  "feeds": { "Beispiel-Podcast": "https://feeds.example.com/beispiel.xml" },',
        '  "episodes": [ … ] }',
        "```",
        "",
        "Das ersetzt die Suche über Apple — und die ist die unzuverlässigste",
        "Stufe der Kette, weil sie am Namen hängt und der dort manchmal anders",
        "geschrieben steht als bei Spotify.",
        "",
        "Am einfachsten geht das ohne jede Vorbereitung: ein `manifest.json`",
        "aus einem früheren Lauf kann unverändert in das Feld eingeworfen",
        "werden. Dort steht bei jeder Folge schon Sendung und Feed, und Fundus",
        "schreibt die Tabelle zusätzlich ausdrücklich hinein. Fundus merkt sich",
        "die Feeds absichtlich nicht selbst — das Wissen bleibt in den Dateien",
        "des Nutzers, und von dort kann es auch dir gegeben werden.",
        "",
        "## Regeln, die aus Fehlern kommen",
        "",
        "1. **Nichts erfinden.** Ein falscher Feed oder eine falsche GUID ist",
        "   schlechter als ein fehlendes Feld: Fundus vertraut der GUID und",
        "   prüft sie nicht gegen den Titel. Weißt du einen Wert nicht, lass",
        "   das Feld weg.",
        "2. **Dauer in Sekunden.** Spotify liefert Millisekunden. Wer sie",
        "   übernimmt, liegt um den Faktor 1000 neben der",
        "   Fünf-Sekunden-Toleranz des Abgleichs, und die Folge wird nie",
        "   gefunden.",
        "3. **Eine Zeile pro Folge, Reihenfolge egal.** Zehn Folgen in einem",
        "   Zettel sind der Normalfall, nicht die Ausnahme.",
        "4. **Spotify-Links nur zur Identifikation.** Das Audio holt Fundus",
        "   aus dem RSS-Feed; Spotify-Transkripte sind ohne Login nicht",
        "   erreichbar.",
        "5. **Keine Nutzerdaten, keine Tokens.** Der Zettel enthält Links und",
        "   Metadaten, sonst nichts.",
        "",
        "## Was danach passiert",
        "",
        "Fundus löst jede Folge auf und schreibt pro Folge Shownotes als",
        "Markdown, ein offizielles Transkript, falls der Feed eines führt",
        "(etwa ein Viertel der Sendungen), und den Ton. Dazu kommt",
        "`manifest.json` mit den Metadaten aller Folgen und, für alles ohne",
        "offizielles Transkript, ein Whisper-Script für macOS und Windows.",
        "In der Mac-App wird stattdessen gleich lokal transkribiert.",
        "",
        "Unsichere Treffer werden als `unsicher` markiert und nicht",
        "stillschweigend übernommen — die musst du oder ein Mensch bestätigen.",
    ]
    if base_url:
        lines += ["", f"Diesen Text gibt es auch unter {base_url.rstrip('/')}"
                      "/api/podcast/auftragszettel.md"]
    return "\n".join(lines) + "\n"
