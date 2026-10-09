"""Die englische Oberfläche ist wirklich englisch.

Übersetzt wird an zwei Stellen: der Server ersetzt beim Rendern Textknoten,
einige Attribute und exakte JS-Stringliterale; alles, was das Script zur
Laufzeit zusammenbaut, läuft über t(). Beide Wege scheitern still — ein
Textknoten, der nicht exakt dem Schlüssel entspricht, bleibt deutsch, ebenso
ein Satz in einem Template-Literal ohne t(). Die Seite sieht dann fast fertig
aus, und genau das hat über Monate niemand bemerkt.

Deshalb wird hier die echte Seite gerendert (Server- und Desktop-Variante)
und auf sichtbares Deutsch durchsucht; dazu kommen die Schlüssel hinter t()
und die MCP-Konfigurationen, die Leute in ihre Programme kopieren.
"""
import html as htmllib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "index.html"
LOCALES = ROOT / "locales"

GERMAN = re.compile(r"[äöüÄÖÜß]|\b(und|nicht|Datei|wird|oder|mit|für|Folge)\b")


# ── Rendering ─────────────────────────────────────────────────────────────────

@pytest.fixture
def render_en(client, alice, admin, as_user, monkeypatch):
    """Return a function that renders /?lang=en in one of three variants."""
    import main

    def _render(variant: str) -> str:
        if variant == "desktop":
            monkeypatch.setattr(main, "DESKTOP_MODE", True)
            monkeypatch.setattr(main, "LOCAL_MODE", True)
            client.cookies.clear()
        else:
            monkeypatch.setattr(main, "DESKTOP_MODE", False)
            monkeypatch.setattr(main, "LOCAL_MODE", False)
            as_user(admin if variant == "admin" else alice)
        r = client.get("/?lang=en")
        assert r.status_code == 200, (variant, r.status_code)
        return r.text
    return _render


def visible_strings(page: str) -> list[str]:
    """Text nodes and the attributes a person reads, with scripts, styles and
    comments removed. The I18N table sits inside a <script> and goes with it."""
    page = re.sub(r"<script\b.*?</script>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<style\b.*?</style>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    out = []
    for m in re.finditer(r">([^<>]+)<", page):
        s = htmllib.unescape(m.group(1)).strip()
        if s:
            out.append(s)
    for m in re.finditer(r'\b(?:placeholder|title|aria-label|alt)="([^"]*)"', page):
        s = htmllib.unescape(m.group(1)).strip()
        if s:
            out.append(s)
    return out


@pytest.mark.parametrize("variant", ["server", "admin", "desktop"])
def test_no_visible_german_in_english_ui(render_en, variant):
    page = render_en(variant)
    offenders = sorted({s for s in visible_strings(page) if GERMAN.search(s)})
    assert not offenders, (
        f"Deutsch in der englischen Oberfläche ({variant}):\n  "
        + "\n  ".join(repr(s[:120]) for s in offenders))


def test_no_visible_german_on_login_page(client, monkeypatch):
    import main
    monkeypatch.setattr(main, "DESKTOP_MODE", False)
    r = client.get("/login?lang=en")
    assert r.status_code == 200
    offenders = sorted({s for s in visible_strings(r.text) if GERMAN.search(s)})
    assert not offenders, offenders


def test_assistant_wording_is_generic(render_en):
    """The settings line used to name two products; it is about assistants."""
    page = render_en("desktop")
    assert "What is installed, and MCP access for AI assistants." in page
    assert "Claude &amp; Codex." not in page


# ── Keys used from JavaScript ─────────────────────────────────────────────────

def _script_source() -> str:
    src = TEMPLATE.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", src, flags=re.S)
    code = "\n".join(blocks)
    # Whole-line comments only: they quote t() in prose ("t('Noch {n} übrig')").
    return "\n".join(l for l in code.split("\n") if not l.strip().startswith("//"))


_LIT = r"""(['"])((?:(?!\1)[^\\\n]|\\.)*)\1"""


def _unquote(s: str) -> str:
    return s.replace("\\'", "'").replace('\\"', '"')


def js_keys() -> set[str]:
    code = _script_source()
    keys = {_unquote(m.group(2)) for m in re.finditer(r"\bt\(\s*" + _LIT, code)}
    for m in re.finditer(r"\bplural\([^,]+,\s*" + _LIT + r"\s*,\s*" + _LIT, code):
        keys.add(_unquote(m.group(2)))
        keys.add(_unquote(m.group(4)))
    return keys


def test_t_literals_have_translations():
    """Every literal handed to t() or plural() must exist in both catalogues —
    a missing key renders German in the English UI, silently."""
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    de = json.loads((LOCALES / "de.json").read_text(encoding="utf-8"))
    keys = js_keys()
    assert len(keys) > 100, "Schlüsselsuche findet zu wenig — Regex kaputt?"
    missing_en = sorted(k for k in keys if k not in en)
    missing_de = sorted(k for k in keys if k not in de)
    assert not missing_en, f"fehlt in en.json: {missing_en}"
    assert not missing_de, f"fehlt in de.json: {missing_de}"


def test_t_is_not_shadowed_where_it_is_called():
    """A local `const t=…` hides the translation function; calling t() in the
    same function then throws (or worse, calls a DOM node). Three functions had
    exactly that before their strings were wrapped."""
    code = _script_source()
    assert not re.search(r"\bconst\s+t\s*=", code), \
        "lokales `const t=` verdeckt die Übersetzungsfunktion"


# ── Download view starts consistent (B1/S1) ──────────────────────────────────

def test_download_view_initial_state_matches_files_mode():
    src = TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"let mode='video'", src)
    assert 'aria-pressed="true" data-mode="video"' in src
    assert 'class="subopts show" id="dlOpts"' in src
    assert '<span id="runLabel">Herunterladen</span>' in src
    assert "video:{label:'Herunterladen'}" in src
    # setMode runs once at start, after the navigation exists.
    assert re.search(r"renderNav\(\);\s*setMode\(mode\);", src)
    assert ".modes.solo" not in src and "opens in text mode" not in src


def test_missing_extractor_forces_text_mode():
    src = TEMPLATE.read_text(encoding="utf-8")
    body = src.split("function applyExtractor(has){", 1)[1].split("\n  }\n", 1)[0]
    assert "el.disabled=!has" in body
    assert "setMode('text')" in body
    assert "applyExtractor(has);" in src


# ── Static script parses ──────────────────────────────────────────────────────

NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="node nicht installiert")
def test_inline_script_is_valid_javascript(tmp_path):
    src = TEMPLATE.read_text(encoding="utf-8")
    code = "\n".join(re.findall(r"<script>(.*?)</script>", src, flags=re.S))
    code = re.sub(r"\{\{.*?\}\}", "null", code, flags=re.S)
    code = re.sub(r"\{%.*?%\}", "", code, flags=re.S)
    f = tmp_path / "inline.js"
    f.write_text(code, encoding="utf-8")
    r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ── MCP snippets ──────────────────────────────────────────────────────────────

TOKEN = "tok_AbC-123_xyz"


def _snippets(tmp_path, origin: str) -> dict:
    src = TEMPLATE.read_text(encoding="utf-8")
    block = src.split("// MCP-SNIPPETS-BEGIN", 1)[1].split("// MCP-SNIPPETS-END", 1)[0]
    block = block.split("\n", 1)[1]           # rest of the marker comment line
    js = (
        "const t=s=>s;\n"
        f"const location={{origin:{json.dumps(origin)}}};\n"
        + block +
        "\nconst out={};\n"
        "for(const [k,v] of Object.entries(MCP_CLIENTS))"
        f" out[k]={{hint:v[0],cfg:v[1](location.origin+'/mcp',{json.dumps(TOKEN)})}};\n"
        "console.log(JSON.stringify(out));\n"
    )
    f = tmp_path / "mcp.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(not NODE, reason="node nicht installiert")
@pytest.mark.parametrize("origin,remote_http", [
    ("https://fundus.example.com", False),
    ("http://127.0.0.1:8765", False),
    ("http://localhost:8765", False),
    ("http://192.168.1.20:8765", True),
])
def test_mcp_snippets_are_valid(tmp_path, origin, remote_http):
    import tomllib
    url = origin + "/mcp"
    s = _snippets(tmp_path, origin)

    # Claude Code: one line, so it pastes into PowerShell and cmd as well.
    cc = s["claude-code"]["cfg"]
    assert "\n" not in cc and "\\" not in cc
    assert cc.startswith("claude mcp add --transport http")
    assert f'--header "Authorization: Bearer {TOKEN}"' in cc and url in cc

    # Codex: real TOML, token in a header — never in a comment.
    cx = s["codex"]["cfg"]
    d = tomllib.loads(cx)["mcp_servers"]["fundus"]
    assert d["url"] == url
    assert d["http_headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert not any(l.lstrip().startswith("#") for l in cx.splitlines())
    assert "bearer_token_env_var" in s["codex"]["hint"]

    # Claude Desktop: JSON, mcp-remote, --allow-http exactly when needed.
    cd = json.loads(s["claude-desktop"]["cfg"])["mcpServers"]["fundus"]
    assert cd["command"] == "npx" and url in cd["args"]
    assert ("--allow-http" in cd["args"]) is remote_http
    assert cd["env"]["FUNDUS_AUTH"] == f"Bearer {TOKEN}"
    assert ("--allow-http" in s["claude-desktop"]["hint"]) is remote_http

    js = json.loads(s["json"]["cfg"])["mcpServers"]["fundus"]
    assert js["url"] == url and js["headers"]["Authorization"] == f"Bearer {TOKEN}"

    yaml = pytest.importorskip("yaml")
    g = yaml.safe_load(s["goose"]["cfg"])["extensions"]["fundus"]
    assert g["uri"] == url and g["headers"]["Authorization"] == f"Bearer {TOKEN}"

    ow = s["openwebui"]["cfg"]
    assert f"URL:   {url}" in ow and f"Key:   {TOKEN}" in ow

    other = s["other"]["cfg"]
    assert "\n" not in other and other.startswith("npx -y mcp-remote ")
    assert ("--allow-http" in other) is remote_http
    assert ("--allow-http" in s["other"]["hint"]) is remote_http


# ── Messages produced by the server ──────────────────────────────────────────
# The page can be perfectly English and still show German the moment something
# goes wrong: error details, job stages and notes are written in German where
# they arise and reach the UI as data, not as markup.

import ast  # noqa: E402

MAIN = ROOT / "main.py"


def _lang_client(client, alice, as_user, lang: str):
    c = as_user(alice)
    c.cookies.set("hd_lang", lang)
    return c


@pytest.mark.parametrize("lang,expected", [
    ("en", "No episodes recognised. Paste links or an order sheet in the fundus-podcast/1 format."),
    ("de", "Keine Folgen erkannt. Links einfügen oder Auftragszettel im Format fundus-podcast/1."),
])
def test_http_exception_detail_follows_language(client, alice, as_user, lang, expected):
    c = _lang_client(client, alice, as_user, lang)
    r = c.post("/api/podcast", data={"urls": ""})
    assert r.status_code == 400
    assert r.json()["detail"] == expected


def test_http_exception_keeps_status_and_body_shape(client, alice, as_user):
    """Only the text changes: status codes stay, and a detail that is not in
    the catalogue (FastAPI's own "Not Found") passes through untouched."""
    c = _lang_client(client, alice, as_user, "en")
    r = c.get("/api/definitely-not-a-route")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    r = c.get("/api/podcast/does-not-exist")
    assert r.status_code == 404 and r.json() == {"detail": "Not found."}


def test_job_error_and_stage_come_back_english(client, alice, as_user):
    import main
    jid = "en-job-test"
    main.jobs[jid] = {"user_id": alice["id"], "status": "error",
                      "stage": "Untertitel werden abgerufen…",
                      "error": "Beitrag nicht gefunden (gelöscht oder privat?)."}
    try:
        c = _lang_client(client, alice, as_user, "en")
        j = c.get(f"/api/job/{jid}").json()
        assert j["error"] == "Post not found (deleted or private?)."
        assert j["stage"] == "Fetching subtitles…"
        # The stored job is a copy-on-read: German callers still get German.
        assert main.jobs[jid]["error"].startswith("Beitrag nicht gefunden")
        c.cookies.set("hd_lang", "de")
        assert c.get(f"/api/job/{jid}").json()["error"].startswith("Beitrag nicht gefunden")
    finally:
        main.jobs.pop(jid, None)


def test_placeholder_message_keeps_its_number():
    import i18n
    msg = ("Abgebrochen nach 120 Sekunden — die Plattform antwortet nicht oder blockt. "
           "Mit hinterlegten Cookies geht es meist sofort; sonst den Whisper-Befehl "
           "unten lokal ausführen.")
    out = i18n.translate_message(msg, "en")
    assert out.startswith("Cancelled after 120 seconds — "), out
    assert i18n.translate_message("Abgebrochen nach 45 Sekunden.", "en") == \
        "Cancelled after 45 seconds."
    # The longer pattern wins over the shorter one it contains.
    assert i18n.translate_message("12 Folgen — höchstens 5 auf einmal.", "en") == \
        "12 episodes — at most 5 at once."
    assert i18n.translate_message("12 Folgen — höchstens 5.", "en") == \
        "12 episodes — at most 5."
    # Unknown text passes through untouched; German stays German.
    assert i18n.translate_message("ERROR: [youtube] xyz: boom", "en") == \
        "ERROR: [youtube] xyz: boom"
    assert i18n.translate_message("Abgebrochen nach 45 Sekunden.", "de") == \
        "Abgebrochen nach 45 Sekunden."


def _http_exception_details():
    """(line, text, is_fstring) for every literal detail handed to
    HTTPException in main.py. f-strings get a sample value per expression."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    out = []

    def render(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, False
        if isinstance(node, ast.JoinedStr):
            return "".join(v.value if isinstance(v, ast.Constant) else "7"
                           for v in node.values), True
        return None, False

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("HTTPException"):
            args = list(n.args[1:2]) + [k.value for k in n.keywords if k.arg == "detail"]
            for a in args:
                text, is_f = render(a)
                if text is not None:
                    out.append((n.lineno, text, is_f))
    return out


def test_every_german_http_detail_has_a_translation():
    import i18n
    details = _http_exception_details()
    assert len(details) > 40, "HTTPException-Suche findet zu wenig"
    missing = []
    for line, text, _ in details:
        if not GERMAN.search(text) and not re.search(r"\b(Nicht|Nur|Kein|Unbekannt)", text):
            continue                       # already English
        if i18n.translate_message(text, "en") == text:
            missing.append(f"main.py:{line}: {text!r}")
    assert not missing, "ohne englische Übersetzung:\n  " + "\n  ".join(missing)


def test_mcp_tool_results_are_translated(monkeypatch):
    """The registered tool is what a client calls; its errors arrive in the
    instance language (English by default), not in the German source."""
    import asyncio
    import i18n
    import mcp_tools
    monkeypatch.setattr(i18n, "DEFAULT_LANG", "en")
    tool = next(t for t in mcp_tools.mcp._tool_manager.list_tools()
                if t.name == "get_podcast_package_status")
    tok = mcp_tools.current_mcp_user.set(None)
    try:
        out = asyncio.run(tool.fn(podcast_id="nope"))
    finally:
        mcp_tools.current_mcp_user.reset(tok)
    assert out["error"] == "No user. Check the bearer token."
