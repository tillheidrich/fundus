"""Shared fixtures — above all an HTTP client.

Until this file existed, every authorisation claim in the suite was a
`"_own_trim" in inspect.getsource(fn)`. That passes when the name sits in a
comment, and it would have passed for a `scrub()` call whose function did not
exist — which happened, and stayed green, because nothing ever executed that
path.

With a client, "user B cannot read user A's files" becomes a request and a
status code instead of a string search.
"""
import os
import tempfile
from pathlib import Path

import pytest

# Isolate the database before main is imported: db.py resolves DATA_DIR at
# import time, and a test run must never touch a real one.
_TMP = tempfile.mkdtemp(prefix="fundus-tests-")
os.environ.setdefault("DATA_DIR", str(Path(_TMP) / "data"))
os.environ.setdefault("DOWNLOAD_DIR", str(Path(_TMP) / "downloads"))


@pytest.fixture(autouse=True)
def _fresh_hourly_budget():
    """The hourly job budget lives in the process. Transcript, trim and
    podcast requests count against it now, and the same few accounts make
    all of them across the suite — without a reset, test order would decide
    which one first meets a 429."""
    import sys
    m = sys.modules.get("main")
    if m is not None:
        m._batch_calls.clear()
    yield


@pytest.fixture
def client():
    """A client that does not run the application lifespan.

    Deliberately no `with` block: the lifespan starts the MCP session manager,
    which refuses to run twice per instance, so a context-managed client works
    for exactly one test and errors in every one after it. Nothing here needs
    the background loops — they only prune files and poll for updates.
    """
    from fastapi.testclient import TestClient
    import main
    main.db.init_db()

    # The peer address matters: by default TestClient reports "testclient",
    # which is not a loopback address, so every desktop-mode path would be
    # skipped and the tests for it would quietly assert nothing.
    #
    # Rewriting the scope rather than passing TestClient's `client=` argument:
    # that argument does not exist in the Starlette version requirements.txt
    # pins, so using it made the suite pass on a developer machine with newer
    # packages and fail everywhere the pins are honoured — including CI.
    async def loopback(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": ("127.0.0.1", 51234)}
        await main.app(scope, receive, send)

    # follow_redirects off: a redirect to /login answers 200, which hides the
    # 302 that is the thing worth checking.
    # base_url: the desktop gate checks the Host header (DNS rebinding), and
    # TestClient would otherwise send "testserver".
    c = TestClient(loopback, base_url="http://127.0.0.1:8765", follow_redirects=False)
    try:
        yield c
    finally:
        c.close()


def _make_user(name: str, admin: bool = False) -> dict:
    """An account straight through the database — registration is gated, and
    these tests are not about that gate."""
    import main
    existing = next((u for u in main.db.list_users() if u["username"] == name), None)
    user = existing or main.db.create_user(name, "testpassword")
    # Set the flag either way. The first account in an empty database becomes
    # an administrator — sensible for a real install, and the reason the
    # "ordinary user is refused" tests passed for the wrong reason until the
    # database was properly isolated: `alice` was simply never the first
    # account in the repository's own data directory.
    want = 1 if admin else 0
    if bool(user.get("is_admin")) != bool(want):
        with main.db._conn() as c:
            c.execute("UPDATE users SET is_admin=? WHERE id=?", (want, user["id"]))
        user = main.db.get_user(user["id"])
    return user


@pytest.fixture
def alice(client):
    return _make_user("alice")


@pytest.fixture
def bob(client):
    return _make_user("bob")


@pytest.fixture
def admin(client):
    return _make_user("chief", admin=True)


@pytest.fixture
def as_user(client):
    """Return a callable that signs the client in as a given user.

    Sets the signed session cookie directly rather than posting credentials —
    the login flow has its own tests, and going through it here would couple
    every authorisation test to it.
    """
    import main

    def _login(user: dict):
        client.cookies.set("hd_session", main._sign(user["id"]))
        return client
    return _login
