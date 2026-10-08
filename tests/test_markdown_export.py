"""Markdown is the transcript format people ask for first."""
import main
from tests.test_local_mode import probe


SEGS = [{"start": 0.0, "dur": 2.0, "text": "Moin."},
        {"start": 65.0, "dur": 3.0, "text": "Jetzt geht es um Preise."}]


def test_episode_markdown_has_source_and_time_marks():
    md = main.episode_transcript_md(
        {"title": "Folge 7", "show": "Werkstattfunk", "date": "2026-10-01",
         "link": "https://example.com/7"}, SEGS, "Whisper (turbo)")
    assert md.startswith("# Folge 7")
    for part in ("Werkstattfunk", "2026-10-01", "https://example.com/7", "Whisper (turbo)"):
        assert part in md
    assert "1:05" in md and "Jetzt geht es um Preise." in md


def test_transcript_view_offers_markdown_in_one_click():
    html = (main.Path(main.__file__).parent / "templates/index.html").read_text()
    assert "quickExport('md')" in html
    assert '<option value="md" selected>' in html


def test_desktop_registers_the_download_tools_but_keeps_them_off():
    """Since 1.1.0 the media tools are off on the desktop too. They are
    registered there (a client's tool list is fixed at connect time) and
    refuse at call time until switched on in System → Media.

    Plain LOCAL_MODE is not the desktop app: it also turns up in container
    .env files, and there the media tools stay an operator decision."""
    enabled = "__import__('mcp_tools').MEDIA_TOOLS_ENABLED"
    offered = "'get_download_script' in __import__('mcp_tools').registered_tool_names()"
    desktop = {"LOCAL_MODE": "1", "FUNDUS_DESKTOP": "1", "TRUSTED_PROXIES": ""}
    assert probe(desktop, enabled) == "False"
    assert probe(desktop, offered) == "True"
    assert probe({"LOCAL_MODE": "1", "FUNDUS_DESKTOP": ""}, offered) == "False"
    assert probe({"FUNDUS_DESKTOP": ""}, offered) == "False"


def test_episode_markdown_labels_are_english():
    """The file leaves the app; its labels must not depend on the UI language."""
    md = main.episode_transcript_md(
        {"title": "Folge 7", "show": "Werkstattfunk", "date": "2026-10-01",
         "link": "https://example.com/7"}, SEGS, "Official transcript from the show")
    for label in ("**Show:**", "**Date:**", "**Link:**", "**Transcript source:**"):
        assert label in md
    for german in ("Sendung", "Datum", "Quelle des Transkripts"):
        assert german not in md
    assert main.episode_transcript_md({}, SEGS).startswith("# Transcript")


def test_mcp_path_without_slash_is_not_redirected(client):
    """Several clients do not follow a 307 on POST, or drop the bearer token
    while doing so. /mcp has to answer itself (here: 401 without a token)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code != 307
    assert r.status_code == 401


def test_mcp_tool_descriptions_are_english():
    """Small open models choose tools far better from English descriptions."""
    import mcp_tools
    for t in mcp_tools.mcp._tool_manager.list_tools():
        assert not any(w in t.description for w in ("Hole ", "Baue ", "Liste ", "Löse ")), t.name
    assert mcp_tools.INSTRUCTIONS.startswith("Fundus")
