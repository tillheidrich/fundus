"""Translation.

Keys are the German source strings, gettext-style. That looks odd in a project
whose documentation is English, and it is a deliberate trade: the UI was built
in German, so keying on the source text made the migration a mechanical
find-and-replace instead of 282 judgement calls about naming. A missing key
also degrades to something readable rather than to `nav.download.label`.

Resolution order for a request:
  1. ?lang=xx            — a link someone shared, wins once
  2. Cookie              — what this person chose last time
  3. Accept-Language     — what their browser says
  4. DEFAULT_LANG        — what the operator set
"""
import json
import re
from pathlib import Path

LOCALE_DIR = Path(__file__).parent / "locales"
import os

# The interface is written in German, so German is the source: its catalogue
# is the identity and needs no translation pass.
SOURCE_LANG = "de"
# What a visitor gets when nothing says otherwise. English, because the
# project is published for an international audience; an operator can set
# FUNDUS_LANG=de.
DEFAULT_LANG = (os.environ.get("FUNDUS_LANG", "en").strip().lower() or "en")[:2]
COOKIE_NAME = "hd_lang"

_cache: dict[str, dict[str, str]] = {}


def available() -> list[str]:
    """Languages shipped, sorted. Adding one means dropping in a JSON file."""
    if not LOCALE_DIR.is_dir():
        return [DEFAULT_LANG]
    return sorted(p.stem for p in LOCALE_DIR.glob("*.json"))


def catalog(lang: str) -> dict[str, str]:
    lang = normalize(lang)
    if lang not in _cache:
        path = LOCALE_DIR / f"{lang}.json"
        try:
            _cache[lang] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            _cache[lang] = {}
    return _cache[lang]


def normalize(lang: str) -> str:
    """'de-AT' → 'de'. Unknown languages fall back rather than 404."""
    base = (lang or "").strip().lower().split("-")[0].split("_")[0]
    return base if base in available() else DEFAULT_LANG


def parse_accept_language(header: str) -> str:
    """Pick the best supported language from an Accept-Language header.

    Honours q-values, because browsers use them to express real preference:
    'de;q=0.7, en;q=0.9' means English, even though German is listed first.
    """
    if not header:
        return DEFAULT_LANG
    ranked: list[tuple[float, str]] = []
    for part in header.split(","):
        bits = part.strip().split(";")
        tag = bits[0].strip()
        if not tag:
            continue
        q = 1.0
        for b in bits[1:]:
            m = re.match(r"\s*q\s*=\s*([0-9.]+)", b)
            if m:
                try:
                    q = float(m.group(1))
                except ValueError:
                    q = 0.0
        ranked.append((q, tag))
    for _, tag in sorted(ranked, key=lambda x: -x[0]):
        if tag == "*":
            return DEFAULT_LANG
        base = tag.lower().split("-")[0]
        if base in available():
            return base
    return DEFAULT_LANG


def resolve(query_lang: str = "", cookie_lang: str = "", accept: str = "") -> str:
    """Explicit choice beats a remembered one beats a browser hint."""
    for candidate in (query_lang, cookie_lang):
        if candidate:
            base = (candidate or "").strip().lower().split("-")[0]
            if base in available():
                return base
    return parse_accept_language(accept)


def translate(key: str, lang: str = DEFAULT_LANG) -> str:
    """Untranslated keys return the key itself — a German label in an English
    UI is a visible bug; an empty string is an invisible one."""
    return catalog(lang).get(key, key)


# ── Messages produced by the server ───────────────────────────────────────────
# Error details, job stages and notes are written in German where they arise,
# like the rest of the UI. They are translated on the way out. Many of them are
# f-strings ("Abgebrochen nach 120 Sekunden …"), which an exact lookup cannot
# find, so catalogue keys may carry {name} placeholders: such a key becomes a
# pattern, the placeholders capture whatever stands in their place, and the
# captured values are put into the translation.

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_pattern_cache: dict[str, list[tuple[re.Pattern, str]]] = {}
# A key that is almost all placeholder ("{n} Folgen") would match far too much
# free text; patterns need this much literal text to be trusted.
_MIN_LITERAL = 6


def _patterns(lang: str) -> list[tuple[re.Pattern, str]]:
    if lang in _pattern_cache:
        return _pattern_cache[lang]
    out: list[tuple[int, re.Pattern, str]] = []
    for key, val in catalog(lang).items():
        # ${…} keys are JavaScript template literals, not server messages.
        if key == val or "${" in key or not _PLACEHOLDER.search(key):
            continue
        literal = _PLACEHOLDER.sub("", key)
        if len(literal.strip()) < _MIN_LITERAL:
            continue
        rx, seen, pos = [], set(), 0
        for m in _PLACEHOLDER.finditer(key):
            rx.append(re.escape(key[pos:m.start()]))
            name = m.group(1)
            rx.append(f"(?P={name})" if name in seen else f"(?P<{name}>.+?)")
            seen.add(name)
            pos = m.end()
        rx.append(re.escape(key[pos:]))
        out.append((len(literal), re.compile("".join(rx), re.S), val))
    # Most literal text first: "{n} Folgen — höchstens {max} auf einmal." must
    # win over "{n} Folgen — höchstens {max}." for the longer message.
    out.sort(key=lambda x: -x[0])
    _pattern_cache[lang] = [(p, v) for _, p, v in out]
    return _pattern_cache[lang]


def translate_message(msg, lang: str = DEFAULT_LANG):
    """A server message in the caller's language. Exact catalogue hits first,
    then placeholder patterns; anything unknown (a raw yt-dlp line, an
    exception text) is returned unchanged. Non-strings pass through."""
    if not isinstance(msg, str) or not msg:
        return msg
    lang = normalize(lang)
    if lang == SOURCE_LANG:
        return msg
    cat = catalog(lang)
    hit = cat.get(msg)
    if hit is not None:
        return hit
    stripped = msg.strip()
    if stripped != msg and stripped in cat:
        return cat[stripped]
    for rx, val in _patterns(lang):
        m = rx.fullmatch(msg)
        if m:
            groups = m.groupdict()
            return _PLACEHOLDER.sub(lambda p: groups.get(p.group(1), p.group(0)), val)
    return msg


MESSAGE_FIELDS = ("error", "stage", "note", "detail")


def translate_fields(obj, lang: str = DEFAULT_LANG, fields=MESSAGE_FIELDS):
    """A copy of a status payload with its message fields translated, at any
    depth (a batch carries its jobs, a podcast package its episodes). The
    stored job state is never touched — other requests may want German."""
    if normalize(lang) == SOURCE_LANG:
        return obj
    if isinstance(obj, dict):
        return {k: (translate_message(v, lang) if k in fields and isinstance(v, str)
                    else translate_fields(v, lang, fields))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [translate_fields(v, lang, fields) for v in obj]
    return obj


def catalog_json(lang: str) -> str:
    """The whole catalogue for the browser. The UI is a single page whose
    JavaScript builds most of its own markup, so it needs the strings too.
    ~20 KB uncompressed, a few KB over the wire — cheaper than a round trip
    per label."""
    return json.dumps(catalog(lang), ensure_ascii=False, separators=(",", ":"))


# Locale-dependent formatting the UI used to hardcode to German.
NUMBER_GROUPING = {"de": "de-DE", "en": "en-US"}


def js_locale(lang: str) -> str:
    return NUMBER_GROUPING.get(normalize(lang), "en-US")


# ── Translating the rendered page ─────────────────────────────────────────────
# The alternative was wrapping 282 strings in `{{ t(…) }}` across two template
# files. That is 282 chances to break a working German UI with a stray quote,
# and it makes the templates harder to read for no benefit to the reader.
#
# Instead the page renders in German — the source language — and is translated
# on the way out. Only *exact* matches are substituted, never substrings, so
# "Video" inside "Video-URL" is left alone and the yt-dlp command lines and
# shell snippets embedded in this page cannot be corrupted: none of them
# appear in the catalogue.

_TEXT_NODE = re.compile(r">([^<>]+)<")
_ATTR = re.compile(r'\b(placeholder|title|aria-label|alt)="([^"]*)"')
_JS_STRING = re.compile(r"""(['"`])((?:(?!\1)[^\\\n])*)\1""")

_rendered_cache: dict[tuple[int, str], str] = {}


def _sub_text_nodes(html: str, cat: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        raw = m.group(1)
        stripped = raw.strip()
        hit = cat.get(stripped)
        if hit is None or hit == stripped:
            return m.group(0)
        # Keep the original surrounding whitespace — it carries layout.
        lead = raw[:len(raw) - len(raw.lstrip())]
        tail = raw[len(raw.rstrip()):]
        return f">{lead}{hit}{tail}<"
    return _TEXT_NODE.sub(repl, html)


def _sub_attributes(html: str, cat: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        hit = cat.get(m.group(2))
        return f'{m.group(1)}="{hit}"' if hit else m.group(0)
    return _ATTR.sub(repl, html)


def _sub_js_strings(script: str, cat: dict[str, str]) -> str:
    """Quoted literals inside <script>. Exact matches only, and the original
    quote character is preserved so an apostrophe in a translation cannot
    terminate the string early."""
    def repl(m: re.Match) -> str:
        quote, body = m.group(1), m.group(2)
        hit = cat.get(body)
        if hit is None or hit == body:
            return m.group(0)
        if quote in hit:                     # would break out of the literal
            return m.group(0)
        return f"{quote}{hit}{quote}"
    return _JS_STRING.sub(repl, script)


def translate_html(html: str, lang: str) -> str:
    """Render-once, translate-per-language. Cached on the source's identity."""
    lang = normalize(lang)
    if lang == SOURCE_LANG:
        return html
    cat = catalog(lang)
    if not cat:
        return html

    key = (hash(html), lang)
    cached = _rendered_cache.get(key)
    if cached is not None:
        return cached

    # Pass 0: phrases that contain their own markup — "Cookies brauchst du nur
    # für <b>private</b> Posts". These are not a single text node, so the node
    # pass below cannot see them. They are long and contain tags, which makes a
    # plain string replace unambiguous; sorting by length first stops a shorter
    # phrase from eating part of a longer one.
    # Not `key`: that name holds the cache key a few lines up, and reusing it
    # here meant the result was filed under a translation string while lookups
    # used the tuple. The cache never hit, so every request re-ran the whole
    # multi-pass rewrite of a 60 KB page.
    rich = sorted((k for k in cat if "<" in k and cat[k] != k), key=len, reverse=True)
    for phrase in rich:
        if phrase in html:
            html = html.replace(phrase, cat[phrase])

    # Scripts are handled with their own rules; splitting keeps the text-node
    # pass from mangling JavaScript that happens to sit between two tags.
    parts = re.split(r"(<script\b[^>]*>.*?</script>)", html, flags=re.S | re.I)
    out = []
    for part in parts:
        if part[:7].lower() == "<script":
            # partition returns (before, separator, after) — binding the
            # separator to the body was a silent no-op: every script block came
            # back untranslated because `body` was the single character ">".
            head, sep, body = part.partition(">")
            close = body.rfind("</script")
            out.append(head + sep + _sub_js_strings(body[:close], cat) + body[close:]
                       if close != -1 else part)
        else:
            out.append(_sub_attributes(_sub_text_nodes(part, cat), cat))
    result = "".join(out)

    if len(_rendered_cache) > 32:            # a handful of pages × languages
        _rendered_cache.clear()
    _rendered_cache[key] = result
    return result
