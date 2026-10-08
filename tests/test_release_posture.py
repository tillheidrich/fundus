"""Defaults and packaging as published.

These are not correctness tests. They hold a set of deliberate choices in
place — the kind that a later refactor quietly reverses because reversing
them makes something more convenient.

The reasoning, once, here rather than repeated in every docstring: this
project is published from Germany, where the courts have held (OLG Hamburg
5 U 54/23, final since October 2025) that a platform's URL-signing counts as
an effective technical measure. § 95a(3) UrhG then targets *distributing* a
circumvention tool, which is unaffected by whether anyone runs a public
instance. What is actually in the artifact, what a fresh install does before
anyone configures it, and how the project describes itself therefore matter
more than any disclaimer does.
"""
import re
from pathlib import Path

import pytest

import main

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
ENTRYPOINT = (ROOT / "docker-entrypoint.sh").read_text()
README = (ROOT / "README.md").read_text()


# ── The artifact ──────────────────────────────────────────────────────────────

def test_image_does_not_bundle_an_extractor():
    """An image containing the tool distributes it rather than linking to it.
    This is the single most consequential line in the Dockerfile."""
    for pkg in ("yt-dlp", "yt_dlp", "gallery-dl", "gallery_dl"):
        assert f"install {pkg}" not in DOCKERFILE
        assert f'"{pkg}[default]"' not in DOCKERFILE
    assert "yt-dlp[default]" not in DOCKERFILE


def test_entrypoint_requires_an_explicit_opt_in():
    """Supplying the extractor is the operator's decision, made once and
    visibly — not a side effect of pulling an image."""
    assert "EXTRACTOR_AUTO_INSTALL" in ENTRYPOINT
    assert ':-0}" = "1"' in ENTRYPOINT or ":-0} = 1" in ENTRYPOINT


def test_entrypoint_explains_the_alternative():
    """Someone who does not want the auto-install needs to know the other way
    in, or the message reads as a dead end."""
    assert "/usr/local/bin/yt-dlp" in ENTRYPOINT


def test_health_check_does_not_require_the_extractor():
    """An instance doing transcripts only is healthy; reporting it as broken
    would push people toward installing things they do not need."""
    health = [l for l in DOCKERFILE.splitlines() if "HEALTHCHECK" in l or "CMD curl" in l]
    assert health and not any("yt-dlp" in l for l in health)


# ── What a fresh install does ─────────────────────────────────────────────────

def test_youtube_media_is_off_until_switched_on():
    """Nothing is preconfigured for the awkward path. Transcripts, captions
    and metadata — the things this exists for — work out of the box."""
    assert main.ENABLE_YOUTUBE_VIDEO is False


def test_extractor_updates_are_off_by_default():
    """Keeping third-party tooling current is a maintenance decision. An
    install that never asked for it must not acquire the habit on restart."""
    assert main.UPDATE_INTERVAL_H == 0


def test_boot_does_not_update_when_updates_are_disabled():
    """The scheduled loop respects the setting; the boot path has to as well,
    or a restart updates behind the operator's back."""
    src = __import__("inspect").getsource(main._boot)
    assert "UPDATE_INTERVAL_H > 0" in src


def test_signup_still_closes_after_the_first_account(monkeypatch):
    monkeypatch.setattr(main.db, "user_count", lambda: 1)
    monkeypatch.setattr(main, "SIGNUP_CODE", "")
    monkeypatch.setattr(main, "OPEN_SIGNUP", False)
    assert main._signup_mode() == "closed"


def test_no_public_instance_is_configured():
    """There is no hosted instance and the README says so; a default pointing
    at one would contradict that."""
    assert "no public instance" in README.lower() or "There is no hosted" in README


# ── How the project describes itself ──────────────────────────────────────────

def test_readme_leads_with_transcripts():
    """The first paragraph is what a reader takes away, and under
    § 95a(3) Nr. 1 UrhG how a tool is promoted is itself a criterion — what
    the description promises carries weight next to what the code can do."""
    head = README[:1200].lower()
    assert "transcript" in head
    assert head.index("transcript") < 400


def test_readme_does_not_market_media_downloading():
    """Not a euphemism exercise: the feature list below is honest about what
    the tool can do. It is the headline claim that must not be an offer to
    circumvent."""
    head = README[:900].lower()
    for phrase in ("download youtube", "youtube downloader", "download videos from",
                   "rip ", "stream ripper", "mp3 converter", "clipgrab"):
        assert phrase not in head, f"Headline verspricht {phrase!r}"


def test_readme_carries_a_legal_section():
    assert "## Legal" in README
    for point in ("no public instance", "not legal advice", "not affiliated",
                  "95a", "1201"):
        assert point.lower() in README.lower(), point


def test_readme_disclaims_drm_circumvention():
    assert "contains no code for defeating encryption or DRM" in README


def test_readme_drops_the_developer_tools_argument():
    """German courts rejected exactly this argument (OLG Hamburg 5 U 54/23).
    Repeating it reads as a defence that has already lost."""
    assert "developer tools" not in README.lower()
    assert "caches nothing" not in README.lower()


def test_contributing_rejects_infringement_focused_sites():
    """youtube-dl's long-standing policy, and the EFF cited exactly this kind
    of enforced rule when defending it in 2020."""
    c = (ROOT / "CONTRIBUTING.md").read_text().lower()
    assert "dedicated to infringement" in c or "dedicated to distribut" in c
    assert "drm" in c


def test_license_is_agpl():
    """Matches the peer group, and the image carries GPL-licensed ffmpeg
    builds regardless — an MIT project shipping one is an awkward mix."""
    lic = (ROOT / "LICENSE").read_text()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in lic
    assert "Version 3" in lic


# ── Release mechanics ─────────────────────────────────────────────────────────

CI = ROOT / ".github/workflows/build.yml"


def test_ci_runs_tests_before_publishing():
    src = CI.read_text()
    assert "needs: test" in src


def test_images_are_published_only_from_tags():
    """A :latest nobody tagged is how people end up running a half-finished
    commit."""
    assert "startsWith(github.ref, 'refs/tags/v')" in CI.read_text()


def test_ci_builds_for_both_architectures():
    assert "linux/amd64,linux/arm64" in CI.read_text()


def test_blank_issues_are_disabled():
    """Routing extractor reports upstream helps the reporter more than a
    triage label here would."""
    cfg = (ROOT / ".github/ISSUE_TEMPLATE/config.yml").read_text()
    assert "blank_issues_enabled: false" in cfg
    assert "yt-dlp" in cfg


REQ = (ROOT / "requirements.txt").read_text()


def test_requirements_do_not_pull_an_extractor_into_the_image():
    """The Dockerfile installs requirements.txt. Checking the Dockerfile text
    alone was not enough: the extractors came in through this file, and the
    test above stayed green while the image contained them."""
    for pkg in ("yt-dlp", "yt_dlp", "gallery-dl", "gallery_dl", "bgutil"):
        assert not any(l.strip().startswith(pkg) for l in REQ.splitlines()), pkg


def test_extractors_have_their_own_file():
    ext = (ROOT / "requirements-extractors.txt").read_text()
    assert "yt-dlp[default]" in ext and "gallery-dl" in ext
    assert "requirements-extractors.txt" in ENTRYPOINT
    # The app bundles the file, but since 1.1.0 installs it only on explicit
    # consent through the server (POST /api/media/install), never at startup.
    assert "requirements-extractors.txt" in (ROOT / "macos/build-app.sh").read_text()
    assert "requirements-extractors.txt" in (ROOT / "main.py").read_text()


def test_image_contains_no_extractor_plugin():
    assert "bgutil" not in DOCKERFILE


# Helpers whose purpose is getting past a platform's abuse defences (PO-token
# provider, rotating or public proxy pools) are not part of the published
# code. They live in an optional private module the export never ships.
_EVASION_MARKERS = ("POT_BASE_URL", "bgutil", "pot-provider", 'profiles: ["pot"]',
                    "FREE_PROXY", "PROXY_POOL", "PROXY_USERNAME", "PROXY_PASSWORD",
                    "WEBSHARE", "webshare")


@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-entrypoint.sh",
                                  ".env.example", "Dockerfile", "main.py",
                                  "mcp_tools.py", "README.md", "README.de.md",
                                  "docs/PRIVACY.md", "THIRD_PARTY_NOTICES.md"])
def test_public_files_carry_no_evasion_helpers(name):
    text = (ROOT / name).read_text()
    found = [m for m in _EVASION_MARKERS if m in text]
    assert not found, f"{name}: {found}"


def test_compose_is_a_single_service():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "profiles:" not in compose
    assert "depends_on" not in compose


def test_private_module_is_optional():
    """main.py must import cleanly without private_ext and fall back to no-ops."""
    src = (ROOT / "main.py").read_text()
    assert "import private_ext" in src
    assert "except ImportError:\n    private_ext = None" in src


def test_export_does_not_ship_private_parts():
    """The export is an allowlist; the private files must never be on it."""
    script = ROOT / "tools/export-oss.sh"
    if not script.exists():
        pytest.skip("export script is not part of the exported tree")
    export = script.read_text()
    for private in ("private_ext", "docker-compose.private", "tests_private"):
        assert private not in export.split("FILES=(", 1)[1].split("mkdir", 1)[0], private


def test_extractor_check_does_not_trust_a_marker_file():
    """The data volume outlives the image. A marker saying "installed" on a
    fresh image without the extractor left the server without one."""
    assert ".extractors" not in ENTRYPOINT
    assert "have yt-dlp yt_dlp" in ENTRYPOINT


def test_no_third_party_font_cdn():
    """A CDN font request hands every visitor's IP to that host."""
    for t in ("index.html", "login.html"):
        html = (ROOT / "templates" / t).read_text()
        assert "fonts.bunny.net" not in html and "fonts.googleapis" not in html
        assert "/fonts/space-grotesk" in html
    assert (ROOT / "fonts/OFL-SpaceGrotesk.txt").exists()


def test_no_platform_logos_in_the_ui():
    """Naming a platform describes compatibility; drawing its logo does not
    and is not needed. The platform pills are text only."""
    html = (ROOT / "templates/index.html").read_text()
    a = html.index('<div class="pills">'); b = html.index("</div>", a)
    assert "<svg" not in html[a:b]


def test_smoke_test_uses_an_openly_licensed_video():
    """youtube-dl's 2020 takedown cited tests that fetched commercial music
    videos. Big Buck Bunny is CC-BY."""
    smoke = (ROOT / "tools/smoke.sh").read_text()
    assert "aqz-KE-bpKQ" in smoke and "dQw4w9WgXcQ" not in smoke


# ── Container hardening ───────────────────────────────────────────────────────
# No Docker in CI's test job, so these read the files. They check structure
# (the last USER, parsed compose keys), not prose.

def _last(directive: str) -> str:
    lines = [l.split(None, 1)[1].strip() for l in DOCKERFILE.splitlines()
             if l.strip().upper().startswith(directive + " ")]
    return lines[-1] if lines else ""


def test_image_runs_as_unprivileged_user():
    """yt-dlp, gallery-dl and ffmpeg parse untrusted input; as root, any bug
    in them is root in the container."""
    assert "--uid 10001" in DOCKERFILE
    # The entrypoint starts as root only to hand old volumes over, then drops
    # to uid 10001 before pip, yt-dlp or the server run.
    root_phase = ENTRYPOINT.split('if [ "$(id -u)" = "0" ]; then', 1)[1].split("\nfi", 1)[0]
    assert "setpriv --reuid=10001 --regid=10001" in root_phase
    assert "pip" not in root_phase and "uvicorn" not in root_phase


def test_runtime_installs_target_a_venv_the_app_user_owns():
    """The entrypoint and the self-update pip-install at runtime. As a
    non-root user that only works into an environment that user owns, and
    only reaches the server if PATH puts it first."""
    assert "python -m venv /opt/venv" in DOCKERFILE
    assert "chown -R app:app /opt/venv" in DOCKERFILE
    assert "PATH=/opt/venv/bin:$PATH" in DOCKERFILE
    assert "python3 -m pip install" in ENTRYPOINT


def test_compose_is_hardened_and_local_by_default():
    yaml = pytest.importorskip("yaml")
    app = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["app"]
    assert app["ports"] == ["127.0.0.1:8000:8000"]
    assert app["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in app["security_opt"]
    assert app["env_file"] == [{"path": ".env", "required": False}]
