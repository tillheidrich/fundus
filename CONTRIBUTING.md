# Contributing

Thanks for looking. A few things worth knowing before you spend time.

## Scope

Fundus does transcripts and the handling around them — reading them,
searching them, exporting them, trimming the audio they came from. That is
the whole of it.

It is not a library manager, a subscription downloader, a media server or a
general-purpose archiver. Good projects exist for each of those. Keeping the
edges of this one sharp is what makes it maintainable by one person.

Feature requests without an accompanying pull request are unlikely to be
built. That is not dismissiveness — it is arithmetic.

## What will not be merged

**Support for services dedicated to infringement.** Same policy youtube-dl
has had for years: if a site exists primarily to distribute other people's
work without permission, support for it is not welcome here, and pull
requests adding it will be closed.

**DRM circumvention.** Fundus contains no code for defeating content
protection and will not gain any. If a platform encrypts its media, that is
the end of the conversation as far as this project is concerned.

**Workarounds for platform protection measures.** Pull requests that work
around a platform's protection measures will be closed. That includes
signature or cipher handling, PO tokens, bot-check evasion and proxy rotation
to dodge blocks. Fundus relies on yt-dlp as a separate tool that the user
installs, and it does not maintain code like this itself. If a platform
blocks a request, Fundus reports the error and stops there.

**Anything that turns this into a public service.** There is no hosted
instance and there will not be one. Changes premised on running one — shared
queues, multi-tenancy, billing hooks — are out of scope.

## Before you report a bug

**Extractor problems belong upstream.** Most "it stopped working" reports are
platform changes, and the fix lives in
[yt-dlp](https://github.com/yt-dlp/yt-dlp/issues), not here. Check with the
extractor directly first:

```bash
docker exec -ti fundus sh -c 'yt-dlp -v --simulate "URL"'
```

If that fails the same way, it is not a Fundus bug. If it succeeds and
Fundus still does not, that is exactly the report worth having — please
include both outputs.

**Say which mode you are in.** Server or desktop, the version from
System → What is installed, and whether a JavaScript runtime is present. That
last one is the single most misdiagnosed cause of failure: without it,
platforms behave as though they had blocked you.

## Working on the code

```bash
pip install -r requirements.txt
pytest                                  # 371 tests, no network required
uvicorn main:app --reload
tools/smoke.sh http://127.0.0.1:8000    # end-to-end, does hit the network
```

Tests are expected with changes. The suite avoids the network deliberately,
so it stays fast and does not fail because a platform had a bad afternoon —
`tools/smoke.sh` is where real requests live.

Two conventions that matter more than style:

**Comments explain why, not what.** `_yt_extractor_args(True)` in the
self-test suppresses the browser cookie jar, which reads backwards until you
know the check has to answer "is this address welcome" rather than "can we
get in somehow". That sentence belongs next to the code.

**Tests name the failure they prevent.** A test called
`test_dead_client_is_gone` with a docstring explaining that this client
started returning 403 on a specific date is worth ten assertions that only
say what the code currently does.

## Adding a language

Copy `locales/en.json`, translate the values, leave the keys untouched, open
a pull request. Keep the product name untranslated. Machine translations will
not be merged — a language nobody checks is worse than one that is missing,
because the gaps are invisible.

## License

Contributions are licensed under [AGPL-3.0-only](LICENSE), same as the rest.
