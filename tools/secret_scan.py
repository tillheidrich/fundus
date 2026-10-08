#!/usr/bin/env python3
"""Refuse to publish what must not be public.

Runs in CI on every push and before every export. It looks for two kinds of
thing: credentials (tokens, keys) and traces of a private deployment
(hostnames, addresses, internal identifiers). The second list is kept here
as hashes of lowercase needles plus generic patterns, so the file itself
does not spell out what it is guarding.

    python3 tools/secret_scan.py [path ...]      default: tracked files

Exit status 1 on any finding. A line can opt out with the marker
`secret-scan: allow` when a pattern is quoted on purpose (as in this file).
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",  # secret-scan: allow
    "GitLab token": r"\bglpat-[A-Za-z0-9_-]{20,}",  # secret-scan: allow
    "AWS key": r"\bAKIA[0-9A-Z]{16}\b",  # secret-scan: allow
    "OpenAI/Anthropic key": r"\bsk-(?:ant-)?[A-Za-z0-9_-]{24,}",  # secret-scan: allow
    "Private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",  # secret-scan: allow
    "Slack token": r"\bxox[abpr]-[A-Za-z0-9-]{10,}",  # secret-scan: allow
    "Credentials in URL": r"https?://[^/\s:@]+:[^/\s@]{8,}@",  # secret-scan: allow
    "Hex token assignment": r"(?i)(?:token|secret|password|api_key)\s*[:=]\s*['\"][0-9a-f]{32,}['\"]",  # secret-scan: allow
    "Private macOS path": r"/Users/(?!you\b|example\b)[a-z][a-z0-9_-]+/",  # secret-scan: allow
}

# sha256 of lowercase strings that identify the private deployment. Matching
# is on words and dotted names in each line, so the strings never appear here.
PRIVATE_HASHES = {
    "9de8cc661d93a4b9e57caa9b72173fe69b34ac8bef0ea00dd961cf6a6a9e71f4",
    "6b49b6d9befddb23b34629597b3ecfef1362bff46be23b38543d965a5f85399c",
    "58f4e84a33ece0188249dce6ce3b43b0749eee36f8cc1f870c68b757ffc2cafc",
    "d555504352ad9a5b6eff291d08a0e63c7187b9fabf8b92da2042dfe9444d7cb8",
    "0f36c234d3cb9fd7cb57a49b22f7e46791411021b745668836e30486387813a3",
    "9c9fe717788d424047d34c01ca5fa45489ad5915b7c1229d648bbc5fc14d4915",
    "294aa8d75483b8331e3ba6a7f24aea15202747f36de65197e7bc6194880b2558",
    "c17ff93c942656edca32ab1c0b0b440fbeb88c3f58a8567f9a5d732926f749a1",
}

TEXT_SUFFIXES = {".py", ".md", ".txt", ".html", ".json", ".yml", ".yaml", ".sh",
                 ".swift", ".toml", ".ini", ".example", ".cfg", ".js", ".css", ""}


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [Path(p) for p in out.stdout.splitlines()]


def tokens(line: str):
    for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-_]{3,}", line):
        yield t.lower().strip(".")


def scan(paths: list[Path], extra_hashes: set[str]) -> list[str]:
    hashes = PRIVATE_HASHES | extra_hashes
    found = []
    compiled = {k: re.compile(v) for k, v in PATTERNS.items()}
    for p in paths:
        if p.suffix.lower() not in TEXT_SUFFIXES or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if "secret-scan: allow" in line:
                continue
            for name, rx in compiled.items():
                if rx.search(line):
                    found.append(f"{p}:{n}: {name}")
            for t in tokens(line):
                parts = t.split(".")
                cands = {t} | {".".join(parts[i:]) for i in range(len(parts) - 1)}
                if any(hashlib.sha256(c.encode()).hexdigest() in hashes for c in cands):
                    found.append(f"{p}:{n}: private identifier")
                    break
    return found


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--hashes=")]
    extra = set()
    for a in sys.argv[1:]:
        if a.startswith("--hashes="):
            extra |= set(Path(a.split("=", 1)[1]).read_text().split())
    paths = [Path(a) for a in args] if args else tracked_files()
    expanded = []
    for p in paths:
        expanded += [q for q in p.rglob("*") if q.is_file()] if p.is_dir() else [p]
    found = scan(expanded, extra)
    for f in found:
        print(f)
    print(f"secret-scan: {len(expanded)} files, {len(found)} finding(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
