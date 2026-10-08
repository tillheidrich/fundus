# Security

## Reporting a vulnerability

Please do not open a public issue. Write to **mail@tillheidrich.de** with
"Fundus security" in the subject, a description, and steps to reproduce.
You will get an answer within a week. Fixes are released as soon as they are
ready, and you are credited in the changelog unless you prefer not to be.

## What Fundus does

- Runs on your own machine or server. There is no hosted service and no
  telemetry.
- **Server mode:** accounts with PBKDF2-SHA256 password hashes (600 000
  rounds), signed session cookies, a login throttle, closed registration
  after the first account unless you set an invitation code.
- **Desktop mode (macOS app):** listens on 127.0.0.1 only. No password, but
  every request must carry a per-launch token that only the app's own window
  has; the Host header is checked against DNS rebinding.
- Outbound requests to user-supplied URLs (feeds, previews) refuse private,
  loopback and link-local addresses, follow redirects one hop at a time,
  pin the checked address for the actual connection, and stop at a size
  limit.
- URLs handed to yt-dlp must start with `http(s)://`, so no URL can be read
  as a command-line option. Generated shell scripts quote every input.

## What it does not do

- **Platform cookies and API tokens are stored unencrypted** in
  `data/app.db`. Anyone who can read the data volume can use them. Protect
  the volume and its backups, and prefer cookies from a secondary account.
- No TLS of its own. Put a reverse proxy in front of a server instance and
  set `TRUSTED_PROXIES` to its address.
- No sandboxing of the extractors beyond running them as separate processes.
  yt-dlp and gallery-dl run with the server's permissions.
- The container runs as root. Run it with a read-only root filesystem or a
  user namespace if that matters for your setup.

See [docs/PRIVACY.md](docs/PRIVACY.md) for what is stored and which
connections are made.
