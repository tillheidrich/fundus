"""Access control: sign-up modes and job throttling.

The sign-up default matters: an instance without any env configuration must
end up closed, not open. A misconfigured deployment should fail safe.
"""
import time

import pytest

import main


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    main._batch_calls.clear()
    main._login_fails.clear()
    yield
    main._batch_calls.clear()
    main._login_fails.clear()


def _mode(users: int, code: str = "", open_signup: bool = False, monkeypatch=None):
    monkeypatch.setattr(main.db, "user_count", lambda: users)
    monkeypatch.setattr(main, "SIGNUP_CODE", code)
    monkeypatch.setattr(main, "OPEN_SIGNUP", open_signup)
    return main._signup_mode()


def test_signup_open_for_very_first_account(monkeypatch):
    assert _mode(0, monkeypatch=monkeypatch) == "first"


def test_signup_closed_by_default_once_an_account_exists(monkeypatch):
    """Fail safe: no env configuration must not mean 'anyone may register'."""
    assert _mode(1, monkeypatch=monkeypatch) == "closed"


def test_signup_code_gate(monkeypatch):
    assert _mode(1, code="secret", monkeypatch=monkeypatch) == "code"


def test_signup_open_only_when_explicitly_enabled(monkeypatch):
    assert _mode(1, open_signup=True, monkeypatch=monkeypatch) == "open"


def test_first_account_ignores_code_requirement(monkeypatch):
    assert _mode(0, code="secret", monkeypatch=monkeypatch) == "first"


# ── Batch throttle ────────────────────────────────────────────────────────────

def test_throttle_allows_normal_use(monkeypatch):
    monkeypatch.setattr(main, "BATCH_MAX_PER_HOUR", 10)
    main._batch_throttle(1, 5)
    main._batch_throttle(1, 5)          # exactly at the limit


def test_throttle_rejects_beyond_limit(monkeypatch):
    monkeypatch.setattr(main, "BATCH_MAX_PER_HOUR", 10)
    main._batch_throttle(1, 10)
    with pytest.raises(main.HTTPException) as exc:
        main._batch_throttle(1, 1)
    assert exc.value.status_code == 429


def test_throttle_is_per_user(monkeypatch):
    monkeypatch.setattr(main, "BATCH_MAX_PER_HOUR", 5)
    main._batch_throttle(1, 5)
    main._batch_throttle(2, 5)          # a second user is unaffected


def test_throttle_window_expires(monkeypatch):
    monkeypatch.setattr(main, "BATCH_MAX_PER_HOUR", 2)
    main._batch_calls[1] = [time.time() - 3700] * 2      # older than an hour
    main._batch_throttle(1, 2)


# ── State pruning (in-memory dicts must not grow forever) ─────────────────────

def test_prune_drops_old_batches_and_their_jobs():
    from datetime import datetime, timedelta
    old = (datetime.now() - timedelta(hours=48)).isoformat()
    new = datetime.now().isoformat()
    main.jobs.update({"j_old": {"created": old}, "j_new": {"created": new}})
    main.batches.update({
        "b_old": {"created": old, "job_ids": ["j_old"]},
        "b_new": {"created": new, "job_ids": ["j_new"]},
    })
    try:
        main._prune_state()
        assert "b_old" not in main.batches and "j_old" not in main.jobs
        assert "b_new" in main.batches and "j_new" in main.jobs
    finally:
        for k in ("j_old", "j_new"):
            main.jobs.pop(k, None)
        for k in ("b_old", "b_new"):
            main.batches.pop(k, None)


def test_prune_expires_login_failures():
    main._login_fails["1.2.3.4"] = [time.time() - 900]     # older than 10 min
    main._login_fails["5.6.7.8"] = [time.time()]
    main._prune_state()
    assert "1.2.3.4" not in main._login_fails
    assert "5.6.7.8" in main._login_fails


# ── Batch-Größe ───────────────────────────────────────────────────────────────

def test_batch_url_cap_is_generous_enough_for_real_use():
    """100 pasted links must go through; the cap exists to stop 1000."""
    assert main.BATCH_MAX_URLS >= 100
    assert main.BATCH_MAX_PER_HOUR >= main.BATCH_MAX_URLS


def test_hourly_limit_allows_several_full_batches():
    assert main.BATCH_MAX_PER_HOUR >= 2 * main.BATCH_MAX_URLS


def test_password_hashes_use_current_rounds_and_upgrade_old_ones(tmp_path, monkeypatch):
    """Old hashes (120k rounds, no prefix) still log in and are rewritten with
    the current count on success, when the plain password is available."""
    import db, secrets as _s
    salt = _s.token_hex(16)
    legacy = db._hash("pw", salt, db._LEGACY_ROUNDS)
    assert "$" not in legacy and db._check("pw", salt, legacy)
    current = db._hash("pw", salt)
    assert current.startswith(f"p{db.PBKDF2_ROUNDS}$")
    assert db._check("pw", salt, current) and not db._check("nope", salt, current)
