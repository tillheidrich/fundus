"""The YouTube stack: player clients, local transcription.

Why these tests exist: on 2026-08-17 YouTube started 403-ing every format of
the `android_vr` client. That client had been the standing recommendation for
months and sat first in our config, so every YouTube request failed for weeks
and the symptom ("blocked") was indistinguishable from an IP ban. The lesson
is not "pin the right client" — it is that any hardcoded client list is a
time bomb. These tests guard the shape of the thing, not a lucky value.
"""
import asyncio
import inspect
from pathlib import Path

import pytest

import main


# ── Player clients ────────────────────────────────────────────────────────────

def test_dead_client_is_gone():
    """android_vr returns 403 for every format since 2026-08-17."""
    assert "android_vr" not in main.YT_CLIENTS


def test_clients_are_configurable_without_a_code_change():
    """Client half-life is weeks. Editing Python to react is too slow."""
    assert main.YT_CLIENTS == main.os.environ.get("YT_CLIENTS", "visionos,web")


def test_extractor_args_carry_the_clients():
    args = main._yt_extractor_args()
    assert "--extractor-args" in args
    assert any(main.YT_CLIENTS in a for a in args)


def test_a_client_is_always_requested():
    """Falling back to yt-dlp's bare default loses the tuning entirely."""
    assert "player_client=" in " ".join(main._yt_extractor_args())


def test_extractor_args_work_without_extensions(monkeypatch):
    """The public build has no private_ext; the hook must then be a no-op."""
    monkeypatch.setattr(main, "private_ext", None)
    args = main._yt_extractor_args(True)
    assert args == ["--extractor-args", main.YT_PLAYER_CLIENTS]


# ── Whisper: the path that asks YouTube for nothing ───────────────────────────

def test_whisper_returns_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(main, "WHISPER_ENABLED", False)
    segs, lang = asyncio.run(main._transcript_via_whisper("j", "https://youtu.be/x", "de"))
    assert segs == [] and lang == ""


def test_whisper_never_raises(monkeypatch):
    """It is the last stage. An exception here would mask every earlier reason."""
    monkeypatch.setattr(main, "WHISPER_ENABLED", True)

    async def _boom(*a, **kw):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(main, "_download_audio", _boom)
    segs, lang = asyncio.run(main._transcript_via_whisper("j", "https://youtu.be/x", "de"))
    assert segs == []


def test_whisper_refuses_absurdly_long_media(monkeypatch):
    """A three-hour stream would occupy the only worker for an hour."""
    monkeypatch.setattr(main, "WHISPER_ENABLED", True)
    monkeypatch.setattr(main, "WHISPER_MAX_MINUTES", 45)

    async def _fake_audio(*a, **kw):
        return Path("/tmp/nope.mp3")

    async def _long(*a, **kw):
        return 3 * 3600.0

    called = {"transcribed": False}

    async def _never(*a, **kw):
        called["transcribed"] = True
        return [], ""

    monkeypatch.setattr(main, "_download_audio", _fake_audio)
    monkeypatch.setattr(main, "_media_duration", _long)
    monkeypatch.setattr(main, "_whisper_segments", _never)
    segs, _ = asyncio.run(main._transcript_via_whisper("j", "https://youtu.be/x", "de"))
    assert segs == [] and called["transcribed"] is False


def test_audio_download_asks_for_audio_only():
    """Audio is a tenth the size of the video, and all Whisper needs."""
    import inspect
    src = inspect.getsource(main._download_audio)
    assert "bestaudio" in src
    assert "-x" in src


def test_audio_download_uses_the_youtube_args():
    """Otherwise the fallback fails for exactly the reason the others did."""
    import inspect
    assert "_yt_extractor_args" in inspect.getsource(main._download_audio)


def test_whisper_runs_one_at_a_time():
    """Two concurrent transcriptions would swap a small VPS to death. The
    limit is configurable (WHISPER_CONCURRENCY), but the default is one."""
    import os
    assert isinstance(main._whisper_lock, asyncio.Semaphore)
    if not os.environ.get("WHISPER_CONCURRENCY"):
        assert main.WHISPER_CONCURRENCY == 1
        assert main._whisper_lock._value == 1


def test_whisper_model_is_cached_between_jobs():
    """Reloading per job would add ~10 s and a GB of churn every time."""
    import inspect
    assert "global _whisper_model" in inspect.getsource(main._load_whisper)


# ── YouTube's own translation (adopted from Microsoft's MarkItDown) ───────────

def test_translation_is_opt_in():
    """A real track always beats a machine-translated one — try it first."""
    import inspect
    sig = inspect.signature(main._fetch_transcript_sync)
    assert sig.parameters["translate"].default is False


def test_translation_is_skipped_when_the_language_exists():
    """Translating a language we already have would be a pointless round trip."""
    import inspect
    src = inspect.getsource(main._fetch_transcript_sync)
    assert "prefer_lang not in codes" in src


# ── Self-test endpoint ────────────────────────────────────────────────────────

def test_selftest_requires_admin():
    """It reveals infrastructure detail and runs outbound requests."""
    import inspect
    src = inspect.getsource(main.admin_selftest)
    assert "Depends(require_admin)" in src


def test_selftest_separates_bot_block_from_other_failures():
    """The two need opposite fixes and look identical from the UI."""
    src = __import__("inspect").getsource(main.admin_selftest)
    assert "not a bot" in src


def test_selftest_checks_the_js_runtime():
    """Missing Deno is the most misdiagnosed failure — it looks like a block."""
    assert "deno" in __import__("inspect").getsource(main.admin_selftest)


def test_selftest_downloads_nothing():
    """A diagnostic that pulls a video would itself trip rate limits."""
    assert "--simulate" in __import__("inspect").getsource(main.admin_selftest)


# ── Prüfen ist nicht Ausführen ────────────────────────────────────────────────
#
# Das Security-Review hat das unbeaufsichtigte `pip install --pre` gekippt.
# Diese Tests halten die Trennung fest, die daraus folgt: nachsehen darf
# automatisch laufen, installieren nicht.



def test_check_runs_no_command(monkeypatch):
    """Der Kern. Die Prüfung darf unter keinen Umständen pip anfassen —
    weder zum Installieren noch für `pip list`. Der Unterschied zwischen
    Nachsehen und Ausführen ist genau das, was das Review verlangt hat."""
    called = []

    async def spy(cmd):
        called.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(main, "run_cmd", spy)
    # _installed_pip_versions NICHT ersetzen: das ist genau die Funktion, in
    # der ein Unterprozess stecken könnte. Sie wegzupatchen hieße, die
    # Zusicherung an der interessanten Stelle vorbeizuführen — der Test war
    # zuerst so geschrieben und hätte ein `pip list` dort nie bemerkt.
    # Sie läuft hier echt; ihr Ergebnis ist für die Zusicherung egal.
    monkeypatch.setattr(main.asyncio, "to_thread", _fake_thread(
        b'{"info": {"version": "1.0"}, "releases": {"2026.9.27.232945.dev0": '
        b'[{"upload_time": "2026-09-27T23:29:45"}]}}'))
    asyncio.run(main._check_ytdlp_latest())
    assert called == [], f"Die Versionsprüfung hat Befehle ausgeführt: {called}"


def test_check_sees_a_newer_nightly(monkeypatch):
    monkeypatch.setattr(main, "_installed_pip_versions",
                        lambda: {"yt_dlp": "2026.08.19"})
    monkeypatch.setattr(main.asyncio, "to_thread", _fake_thread(
        b'{"info": {"version": "1.0"}, "releases": {"2026.9.27.232945.dev0": '
        b'[{"upload_time": "2026-09-27T23:29:45"}]}}'))
    asyncio.run(main._check_ytdlp_latest())
    assert main._latest["yt-dlp"]["behind"] is True


def test_version_compare_ignores_zero_padding_and_dev_suffix():
    """yt-dlp meldet »2026.09.27.232945«, PyPI nennt denselben Build
    »2026.9.27.232945.dev0«. Alle Nichtziffern zu entfernen sieht einfacher
    aus und ist falsch: es klebt »09« zu einer anderen Zahl zusammen als »9«
    und behält die 0 aus »dev0«."""
    t = main._version_tuple
    assert t("2026.09.27.232945") == t("2026.9.27.232945.dev0")
    assert t("2026.9.16") > t("2026.9.8")


def test_newest_nightly_is_by_upload_time(monkeypatch):
    """»2026.9.8« steht als Text über »2026.9.16«. Genau dort lügt der
    Textvergleich, und die Prüfung würde eine ältere Version als neueste
    melden."""
    monkeypatch.setattr(main.asyncio, "to_thread", _fake_thread(
        b'{"info": {"version": "9.9"}, "releases": {'
        b'"2026.9.8.dev0":  [{"upload_time": "2026-09-08T00:00:00"}],'
        b'"2026.9.16.dev0": [{"upload_time": "2026-09-16T00:00:00"}]}}'))
    assert asyncio.run(main._pypi_newest("yt-dlp", pre=True)) == "2026.9.16.dev0"


def test_stable_channel_is_used_for_everything_but_ytdlp(monkeypatch):
    """Nur yt-dlp wird mit --pre installiert. Die anderen gegen Nightlies zu
    vergleichen würde ewig »veraltet« melden, und ein Update würde sich
    unbeaufsichtigt Vorabversionen holen — genau der Befund des Reviews."""
    monkeypatch.setattr(main.asyncio, "to_thread", _fake_thread(
        b'{"info": {"version": "1.2.3"}, "releases": {"9.9.9.dev0": '
        b'[{"upload_time": "2026-09-27T00:00:00"}]}}'))
    assert asyncio.run(main._pypi_newest("gallery-dl", pre=False)) == "1.2.3"
    assert main.PIP_COMPONENTS["yt-dlp"]["pre"] is True
    assert all(not m["pre"] for p, m in main.PIP_COMPONENTS.items() if p != "yt-dlp")


def test_check_covers_more_than_ytdlp():
    """Deno und ffmpeg liegen in derselben venv und veralten genauso — nur
    sieht man das am Versionsstand von yt-dlp nicht. Vorher wurde genau ein
    Paket von fünf geprüft."""
    assert {"deno", "static-ffmpeg", "gallery-dl"} <= set(main.PIP_COMPONENTS)


def test_failed_update_is_recorded_not_swallowed(monkeypatch):
    """Der schlimmere der beiden Fehler. Vorher verschluckte
    `except Exception: pass` jeden Fehlschlag, und der Rückgabewert von pip
    wurde nie angesehen — die Oberfläche fragte danach die Version ab, bekam
    die alte und schrieb »ist aktuell«."""
    async def failing(cmd):
        return (1, "", "ERROR: No matching distribution found for yt-dlp")

    monkeypatch.setattr(main, "run_cmd", failing)
    monkeypatch.setattr(main, "_read_ytdlp_version", _noop_str)
    monkeypatch.setattr(main, "_check_ytdlp_latest", _noop_dict)
    main._ytdlp_version["updating"] = False
    asyncio.run(main._self_update())
    assert main._last_update["ok"] is False
    assert "No matching distribution" in main._last_update["detail"]


def test_successful_update_is_recorded_too(monkeypatch):
    async def fine(cmd):
        return (0, "Successfully installed yt-dlp", "")

    monkeypatch.setattr(main, "run_cmd", fine)
    monkeypatch.setattr(main, "_read_ytdlp_version", _noop_str)
    monkeypatch.setattr(main, "_check_ytdlp_latest", _noop_dict)
    main._ytdlp_version["updating"] = False
    asyncio.run(main._self_update())
    assert main._last_update["ok"] is True and main._last_update["detail"] == ""


def test_check_survives_pypi_being_down(monkeypatch):
    """Ein Ausfall bei PyPI darf den Start nicht aufhalten."""
    def boom():
        raise OSError("kein Netz")
    monkeypatch.setattr(main.asyncio, "to_thread", _fake_thread(None, boom))
    asyncio.run(main._check_ytdlp_latest())   # darf nicht werfen


def _fake_thread(payload: bytes | None, raiser=None):
    """asyncio.to_thread durch eine feste Antwort ersetzen, ohne echtes Netz."""
    async def fake(fn, *a, **k):
        if raiser:
            raiser()
        return payload
    return fake


async def _noop_str(*a, **k):
    return ""


async def _noop_dict(*a, **k):
    return {}


def test_installed_versions_come_from_metadata_not_a_subprocess():
    """Die Ergänzung zum Test darüber: auch gelesen wird ohne Unterprozess.
    `pip list` wäre bequem und genau die Grenze, die das Security-Review
    gezogen hat — der Unterschied zwischen Nachsehen und Ausführen."""
    # Ohne Docstring und Kommentare suchen: dort steht die Begründung, warum
    # `pip list` NICHT benutzt wird, und eine reine Textsuche kann die
    # Erklärung nicht von der Sache unterscheiden. Dieselbe Falle wie bei
    # test_loopback_check_ignores_forwarded_headers, dort schon gelöst.
    import ast
    src = inspect.getsource(main._installed_pip_versions).strip()
    fn = ast.parse(src).body[0]
    stmts = [n for n in fn.body
             if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                     and isinstance(n.value.value, str))]
    body = "\n".join(ast.get_source_segment(src, n) or "" for n in stmts)
    body = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    assert "metadata.version" in body
    for forbidden in ("run_cmd", "subprocess", "pip list", "Popen"):
        assert forbidden not in body, f"{forbidden} in der Lesefunktion"
