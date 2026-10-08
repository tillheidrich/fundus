"""Media opt-in (1.1.0, legal review).

The Mac app installs no extractor on its own, YouTube media and the MCP
download helpers are off by default, and each is switched on only with an
explicit, recorded consent. Servers keep their environment variables.
"""
import asyncio
import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import main
import mcp_tools

ROOT = Path(__file__).resolve().parent.parent
KEYS = ("youtube_video", "media_tools", "media_consent_at",
        "youtube_video_consent_at", "media_tools_consent_at")


def _clear_settings():
    with main.db._conn() as c:
        c.executemany("DELETE FROM settings WHERE key=?", [(k,) for k in KEYS])


@pytest.fixture
def desktop(monkeypatch, client):
    monkeypatch.setattr(main, "DESKTOP_MODE", True)
    monkeypatch.setattr(main, "ENABLE_YOUTUBE_VIDEO", False)
    monkeypatch.setattr(mcp_tools, "_DESKTOP", True)
    monkeypatch.setattr(mcp_tools, "MEDIA_TOOLS_ENABLED", False)
    _clear_settings()
    main._media_install.update(status="idle", detail="", at=None)
    yield client
    _clear_settings()
    main._media_install.update(status="idle", detail="", at=None)


@pytest.fixture
def fake_pip(monkeypatch):
    """Record every command instead of running pip."""
    calls = []

    async def run_cmd(cmd):
        calls.append(list(cmd))
        return 0, "2026.10.01", ""
    monkeypatch.setattr(main, "run_cmd", run_cmd)
    return calls


def _extractor(monkeypatch, present: bool):
    monkeypatch.setattr(main, "_extractor_installed", lambda: present)
    monkeypatch.setattr(main, "_extractor_version", lambda: "2026.10.01" if present else "")


# ── Defaults ──────────────────────────────────────────────────────────────────

def test_desktop_environment_switches_nothing_on(tmp_path):
    """LOCAL_MODE/FUNDUS_DESKTOP used to set ENABLE_YOUTUBE_VIDEO and
    ENABLE_MEDIA_TOOLS. Neither may be set by the app's own start any more."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("ENABLE_YOUTUBE_VIDEO", "ENABLE_MEDIA_TOOLS", "TRUSTED_PROXIES")}
    env.update(LOCAL_MODE="1", FUNDUS_DESKTOP="1", DATA_DIR=str(tmp_path / "d"),
               DOWNLOAD_DIR=str(tmp_path / "dl"))
    code = ("import os, main, mcp_tools; print(os.environ.get('ENABLE_YOUTUBE_VIDEO'),"
            " os.environ.get('ENABLE_MEDIA_TOOLS'), main.ENABLE_YOUTUBE_VIDEO,"
            " mcp_tools.MEDIA_TOOLS_ENABLED, main.DESKTOP_MODE)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stderr[-800:]
    assert r.stdout.split() == ["None", "None", "False", "False", "True"]


def test_desktop_defaults_are_off(desktop):
    assert main.youtube_video_enabled() is False
    assert mcp_tools.media_tools_enabled() is False


def test_server_keeps_its_environment_variables(monkeypatch):
    monkeypatch.setattr(main, "DESKTOP_MODE", False)
    monkeypatch.setattr(mcp_tools, "_DESKTOP", False)
    monkeypatch.setattr(main, "ENABLE_YOUTUBE_VIDEO", True)
    monkeypatch.setattr(mcp_tools, "MEDIA_TOOLS_ENABLED", True)
    assert main.youtube_video_enabled() is True
    assert mcp_tools.media_tools_enabled() is True


def test_backend_no_longer_installs_extractors_or_deno():
    swift = (ROOT / "macos/Sources/FundusApp/Backend.swift").read_text()
    code = "\n".join(l for l in swift.splitlines() if not l.strip().startswith("//"))
    assert 'appendingPathComponent("requirements-extractors.txt")' not in code
    assert '"install", "--quiet", "deno"' not in code
    assert "static-ffmpeg" in code          # still needed for trimming/Whisper


# ── Endpoints ─────────────────────────────────────────────────────────────────

def test_status_endpoint(desktop, monkeypatch):
    _extractor(monkeypatch, False)
    d = desktop.get("/api/media/status").json()
    assert d["extractor_installed"] is False
    assert d["extractor_version"] == ""
    assert d["youtube_video_enabled"] is False
    assert d["media_tools_enabled"] is False
    assert d["consent_at"] is None


def test_install_requires_consent(desktop, fake_pip):
    assert desktop.post("/api/media/install").status_code == 400
    assert desktop.post("/api/media/install", data={"consent": "false"}).status_code == 400
    assert fake_pip == []
    assert main.db.get_setting("media_consent_at", "") == ""


def test_install_records_consent_and_runs_pip_in_own_python(desktop, fake_pip, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)     # no deno anywhere
    r = desktop.post("/api/media/install", data={"consent": "true"})
    assert r.status_code == 200
    assert main.db.get_setting("media_consent_at", "")
    pip_calls = [c for c in fake_pip if c[1:3] == ["-m", "pip"]]
    assert all(c[0] == sys.executable for c in pip_calls)
    assert any("-r" in c and c[c.index("-r") + 1].endswith("requirements-extractors.txt")
               for c in pip_calls)
    assert any("--pre" in c and "yt-dlp[default]" in c for c in pip_calls)
    assert any(c[-1] == "deno" for c in pip_calls)
    assert main._media_install["status"] == "done"
    assert desktop.get("/api/media/status").json()["consent_at"]


def test_install_failure_is_reported(desktop, monkeypatch):
    async def run_cmd(cmd):
        return 1, "", "ERROR: No matching distribution found"
    monkeypatch.setattr(main, "run_cmd", run_cmd)
    desktop.post("/api/media/install", data={"consent": "true"})
    assert main._media_install["status"] == "error"
    assert "No matching distribution" in main._media_install["detail"]


def test_youtube_toggle_refused_without_extractor(desktop, monkeypatch):
    _extractor(monkeypatch, False)
    main.db.set_setting("media_consent_at", "2026-10-08T10:00:00")
    r = desktop.post("/api/media/settings", data={"youtube_video": "true", "consent": "true"})
    assert r.status_code == 409
    assert main.youtube_video_enabled() is False


def test_youtube_toggle_refused_without_recorded_consent(desktop, monkeypatch):
    _extractor(monkeypatch, True)
    r = desktop.post("/api/media/settings", data={"youtube_video": "true", "consent": "true"})
    assert r.status_code == 409
    assert main.youtube_video_enabled() is False


def test_youtube_toggle_needs_its_own_acknowledgement(desktop, monkeypatch):
    _extractor(monkeypatch, True)
    main.db.set_setting("media_consent_at", "2026-10-08T10:00:00")
    r = desktop.post("/api/media/settings", data={"youtube_video": "true"})
    assert r.status_code == 400
    assert main.youtube_video_enabled() is False
    r = desktop.post("/api/media/settings", data={"youtube_video": "true", "consent": "true"})
    assert r.status_code == 200 and r.json()["youtube_video_enabled"] is True
    assert main.db.get_setting("youtube_video_consent_at", "")
    # Switching off needs nothing.
    r = desktop.post("/api/media/settings", data={"youtube_video": "false"})
    assert r.status_code == 200 and r.json()["youtube_video_enabled"] is False


def test_youtube_media_refused_while_off(desktop, monkeypatch):
    r = desktop.post("/api/batch", data={"urls": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 403


def test_media_tools_toggle(desktop):
    assert desktop.post("/api/media/settings", data={"media_tools": "true"}).status_code == 400
    r = desktop.post("/api/media/settings", data={"media_tools": "true", "consent": "true"})
    assert r.status_code == 200 and r.json()["media_tools_enabled"] is True
    assert mcp_tools.media_tools_enabled() is True


@pytest.mark.parametrize("method,path,data", [
    ("get", "/api/media/status", None),
    ("post", "/api/media/install", {"consent": "true"}),
    ("post", "/api/media/settings", {"media_tools": "true", "consent": "true"}),
])
def test_endpoints_are_desktop_only(monkeypatch, as_user, alice, fake_pip, method, path, data):
    monkeypatch.setattr(main, "DESKTOP_MODE", False)
    c = as_user(alice)
    r = c.get(path) if method == "get" else c.post(path, data=data)
    assert r.status_code == 404
    assert fake_pip == []


# ── MCP ───────────────────────────────────────────────────────────────────────

def test_mcp_media_tools_refuse_while_disabled(desktop):
    out = asyncio.run(mcp_tools.get_download_script(["https://youtu.be/dQw4w9WgXcQ"]))
    assert out["ok"] is False and out["status_code"] == "disabled"
    out = asyncio.run(mcp_tools.get_livestream_script("https://youtu.be/dQw4w9WgXcQ"))
    assert out["ok"] is False and out["status_code"] == "disabled"
    main.db.set_setting("media_tools", "1")
    out = asyncio.run(mcp_tools.get_download_script(["https://youtu.be/dQw4w9WgXcQ"]))
    assert "script" in out


def test_desktop_registers_media_tools_and_gates_them_at_call_time(monkeypatch):
    """A client's tool list is fixed at connect time, so on the desktop the
    tools are always offered; the switch decides at call time."""
    for k in ("ENABLE_MEDIA_TOOLS", "TRUSTED_PROXIES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOCAL_MODE", "1")
    monkeypatch.setenv("FUNDUS_DESKTOP", "1")
    spec = importlib.util.spec_from_file_location(
        f"mcp_tools_probe_{uuid.uuid4().hex}", mcp_tools.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.MEDIA_TOOLS_ENABLED is False
    assert {"get_download_script", "get_livestream_script"} <= mod.registered_tool_names()


def test_trim_respects_the_youtube_switch(as_user, alice, monkeypatch):
    """The trimmer downloads audio; for YouTube that is the governed download."""
    import main
    monkeypatch.setattr(main, "youtube_video_enabled", lambda: False)
    r = as_user(alice).post("/api/trim", data={"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ"})
    assert r.status_code == 403
