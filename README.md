# Showcase

Upload a self-contained HTML page to a stable slug URL, or publish a versioned deck bundle with a lightweight Preact viewer and offline HTML importer. Useful for agent-generated reports, demos, and presentations you want to self-host.

![Status: experimental](https://img.shields.io/badge/status-experimental-orange) ![License: Apache--2.0](https://img.shields.io/badge/license-Apache--2.0-blue)

Created at [Exowatt](https://exowatt.com).

## Status

Experimental: the server and bundle format work, but deployment, quotas, and authentication need careful operator configuration. This release is not an anonymous public upload host. The earlier internal share-link service, workspace pairing, standalone upload shell script, company branding, company-hosted examples, and rich browser library are not included.

## Install

Requires Python 3.13+, [`uv`](https://docs.astral.sh/uv/), and Node.js 20+ to rebuild the deck viewer. The authentication dependency [`forwarded-auth`](https://github.com/exowatt-oss/forwarded-auth) must be available before installation (pinned git dependency; it publishes first).

```sh
uv sync --locked
cd runtime && npm ci && npm run check && cd ..
```

The committed viewer assets are built from `runtime/` with `npm run build` (no Node build step at server startup). SQLite works for a single server instance; set `DATABASE_URL=postgresql+psycopg://…` for PostgreSQL.

## Usage / Quickstart

For an **offline, loopback-only demo** with no identity provider, opt into both insecure-local switches explicitly:

```sh
FORWARDED_AUTH_MODE=none FORWARDED_AUTH_ALLOW_INSECURE_NONE_MODE=1 \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/> and upload `examples/synthetic-page.html`. The browser stores its owner token locally; you can replace its own uploads at the same slug. Reads, including catalog and deck assets, are public on that server. Do **not** bind this demo mode to a network interface or load real/private content. Generate a deck with `uv run python scripts/showcase-deck init my-deck --title 'Example deck'`, validate with `uv run python scripts/showcase-deck validate my-deck`, then `uv run python scripts/showcase-deck push my-deck --slug my-deck`. The synthetic bundle at `examples/synthetic-deck/` is another starting point; `showcase-deck import source.html destination/` converts an existing single-file slide deck. The deck CLI uses `SHOWCASE_URL` (default `http://127.0.0.1:8000`), `SHOWCASE_OWNER_TOKEN` (or a user-local token file), and optionally `SHOWCASE_AUTH_TOKEN` (OIDC bearer token for writes). Single-file pages can also be uploaded through the `/api/presentations` multipart endpoint with those owner/auth headers.

## Authentication and deployment

**Writes require verified identity and a separate owner token.** `POST`, `PUT`, `PATCH`, and `DELETE` are guarded before routing with [`forwarded-auth`](https://github.com/exowatt-oss/forwarded-auth). By default it fails closed: no issuer/audience/email-domain allowlist means production startup refuses to serve. Configure `FORWARDED_AUTH_MODE=forwarded`, `FORWARDED_AUTH_ISSUER`, `FORWARDED_AUTH_AUDIENCE`, and `FORWARDED_AUTH_ALLOWED_EMAIL_DOMAINS` (use `*` only when truly intentional). The proxy must strip inbound identity headers and block direct backend access; verified JWTs are required by default. Alternatively use `FORWARDED_AUTH_MODE=oidc` with bearer tokens. See the dependency's README/security model for proxy topology and OIDC settings. Never turn on `FORWARDED_AUTH_REQUIRE_JWT=false` or `FORWARDED_AUTH_TRUST_PROXY_HEADERS=true` on an untrusted edge. `none` requires both explicit switches and is only for loopback development. Never bind an insecure demo to a public interface.

`X-Owner-Token` is required when creating or replacing HTML pages and deck versions. It is checked for replacement and deletion, but it is **not** a replacement for verified identity. Ownerless historical rows remain readable but are locked against edits. Anyone allowed to upload can choose new slugs; HTML is served verbatim and may contain scripts. Every slug document and uploaded asset gets `Content-Security-Policy: sandbox allow-scripts` (without `allow-same-origin`) so uploaded scripts have an opaque origin and cannot read the upload UI's owner token from localStorage or use same-origin cookies. This sandbox limits forms, cross-origin `fetch` and custom web fonts (the opaque origin needs an explicit CORS policy for those); test interactive pages before publishing. **Do not host untrusted content on the same origin as other sensitive apps.** Use a dedicated domain and do not put confidential HTML onto a public-read deployment. Older deck versions remain public at their pinned URLs.

## Limits and storage

`MAX_UPLOAD_BYTES` (25 MiB default) limits HTML read size, `MAX_REQUEST_BYTES` (70 MiB default) bounds declared request lengths before parsing, and deck assets are capped at 64 MiB each; bundle/version/slide limits are in `src/app/decks.py`. **These are per-request bounds, not aggregate quotas**: configure reverse-proxy body and rate limits, storage/DB quotas, and a retention/backups policy before allowing external writers. A reverse proxy must also enforce a maximum body size for chunked uploads (missing `Content-Length`), since multipart parsing happens before handler size checks. Public reads include the catalog and deck assets. Avoid embedding credentials in pages. Uploaded HTML/bytes are persisted in the database. Deleting a deck removes its version records, so its assets are no longer served under that slug, but content-addressed asset bytes remain stored for reuse. Anyone who already knows an asset hash can reference it in a new deck and make it public again. **Deletion is not a takedown or secure erasure**; this experimental release has no moderation/purge API. For a legal takedown, restrict writes, remove affected deck/version records and asset rows from the database (including backups under the retention policy), and verify public URLs fail before reopening uploads. No content is bundled with this source export other than the synthetic examples.

## Configuration

Copy `.env.example` and set local `DATABASE_URL`, CORS origin, size bounds, and `FORWARDED_AUTH_*` variables. There are no Exowatt services in the defaults. Health endpoints are `/api/health` and `/api/ready`. Run `uv run pytest -q` for backend tests; optional live PostgreSQL integration needs `TEST_DATABASE_URL` and is skipped without it. The viewer rebuild/check is `cd runtime && npm ci && npm run build && npm run check`.

## License

Original code is licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party notices. The dynamically linked required `psycopg`/`psycopg-binary` dependencies are LGPL-3.0-only; acquire them through normal package installation and observe their licenses. No third-party font or media is bundled.

## Contributing

See [CONTRIBUTING](https://github.com/exowatt-oss/.github/blob/main/CONTRIBUTING.md). Sign off commits with the DCO (`git commit -s`); no CLA.

## Security

Report vulnerabilities via [GitHub private vulnerability reporting](https://github.com/exowatt-oss/showcase/security/advisories/new) once the repository becomes public, or email software@exowatt.com. See the [security policy](https://github.com/exowatt-oss/.github/blob/main/SECURITY.md). Do not open a public security issue.
