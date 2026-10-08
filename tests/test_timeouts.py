"""Time budget for a transcript request.

A refusing platform used to keep the UI spinning for minutes while the
cascade ground through every fallback — for the same result the fast path had
already established.
"""
import asyncio

import pytest

import main


def test_hard_deadline_exists():
    assert main.TRANSCRIPT_TIMEOUT > 0


def test_deadline_is_shorter_than_a_user_gives_up():
    """Anything past ~2 minutes feels broken."""
    assert main.TRANSCRIPT_TIMEOUT <= 120


def test_timeout_produces_an_actionable_result(monkeypatch):
    """On timeout the user must get an explanation, not a stuck spinner."""
    monkeypatch.setattr(main, "TRANSCRIPT_TIMEOUT", 0.05)
    monkeypatch.setattr(main, "WHISPER_TIMEOUT", 0)     # budget is the sum of both

    async def _hang(*a, **kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(main, "_transcript_job_inner", _hang)
    jid = "t_timeout"
    main.jobs[jid] = {"status": "pending"}
    try:
        asyncio.run(main._transcript_job(jid, "https://youtu.be/abc123", "native", None))
        job = main.jobs[jid]
        assert job["status"] == "no_subs"
        assert job["blocked"] is True
        assert "Cookies" in job["error"]        # tells the user what to do
        assert job["progress"] == 100           # spinner stops
    finally:
        main.jobs.pop(jid, None)


def test_whisper_gets_its_own_budget_on_top(monkeypatch):
    """Local transcription is real work, not a hung request — the fast-path
    ceiling must not cut it off mid-run."""
    monkeypatch.setattr(main, "WHISPER_ENABLED", True)
    monkeypatch.setattr(main, "TRANSCRIPT_TIMEOUT", 75)
    monkeypatch.setattr(main, "WHISPER_TIMEOUT", 600)

    captured = {}

    async def _capture(coro, timeout=None):
        captured["timeout"] = timeout
        coro.close()

    async def _noop(*a, **kw):
        pass

    monkeypatch.setattr(main, "_transcript_job_inner", _noop)
    monkeypatch.setattr(main.asyncio, "wait_for", _capture)
    asyncio.run(main._transcript_job("t_budget", "https://youtu.be/x", "native", None))
    assert captured["timeout"] == 675


def test_whisper_budget_is_dropped_when_disabled(monkeypatch):
    """An instance without local transcription must not wait ten extra minutes."""
    monkeypatch.setattr(main, "WHISPER_ENABLED", False)
    monkeypatch.setattr(main, "TRANSCRIPT_TIMEOUT", 75)

    captured = {}

    async def _capture(coro, timeout=None):
        captured["timeout"] = timeout
        coro.close()

    async def _noop(*a, **kw):
        pass

    monkeypatch.setattr(main, "_transcript_job_inner", _noop)
    monkeypatch.setattr(main.asyncio, "wait_for", _capture)
    asyncio.run(main._transcript_job("t_budget2", "https://youtu.be/x", "native", None))
    assert captured["timeout"] == 75


def test_successful_job_is_not_overwritten_by_the_deadline(monkeypatch):
    monkeypatch.setattr(main, "TRANSCRIPT_TIMEOUT", 0.05)
    monkeypatch.setattr(main, "WHISPER_TIMEOUT", 0)

    async def _slow_but_done(job_id, *a, **kw):
        main.jobs[job_id]["status"] = "done"
        await asyncio.sleep(5)

    monkeypatch.setattr(main, "_transcript_job_inner", _slow_but_done)
    jid = "t_done"
    main.jobs[jid] = {"status": "pending"}
    try:
        asyncio.run(main._transcript_job(jid, "https://youtu.be/abc123", "native", None))
        assert main.jobs[jid]["status"] == "done"
    finally:
        main.jobs.pop(jid, None)
