# Simplero → Reporting Pipeline: Connection Plan

**Repo:** `tori` · **Stack:** Python · **Direction:** read-only extract from Simplero
**Status:** plan only — no pipeline code written yet.

---

## 0. Confidence and open questions

This plan was written **without access to the v2 OpenAPI spec**. `simplero.com` is
blocked by this environment's network egress policy, so
`https://simplero.com/api/v2/docs/openapi.json` could not be fetched (CONNECT → 403).

| Claim | Confidence |
|---|---|
| Auth model, mandatory `User-Agent`, error shapes | **High** — documented in Simplero's public API repo |
| Pagination (`page` 0-indexed, `per_page` ≤ 100) | **High** — same source, v1 |
| Resource list and field names | **Medium** — derived from v1; v2 "supports many more resources" |
| v2 endpoint paths and JSON schemas | **Unverified** — must be confirmed against the spec |
| Incremental filters (`updated_since` etc.) | **Unknown** — drives §4, see risk R1 |
| Rate limits | **Unknown** — undocumented, see risk R2 |

**Blocking action:** allowlist `simplero.com` in the environment's network policy
(Claude Code on the web → environment settings), or commit `openapi.json` to the repo.
Everything marked *Unverified* above gets confirmed in Milestone 1 the moment that lands.

---

## 1. Connection fundamentals

**Base URL:** `https://simplero.com/api/v2` (v1 is deprecated but still served.)

**Authentication** — API key, two interchangeable forms:

- HTTP Basic: API key as the **username**, password **empty**
- Header: `X-API-Key: <key>`

Prefer the header — it avoids Basic-auth encoding surprises and keeps the key out of
URL-ish contexts. HTTPS is mandatory.

**Mandatory User-Agent.** Simplero rejects requests without an identifying UA:

```
User-Agent: tori (ai-automation@awesm.com)
```

Missing or malformed → `400 {"error": "Incorrect User-Agent"}`. This is easy to lose
when swapping HTTP clients, so it belongs in the client constructor, not at call sites,
and gets a dedicated regression test.

**Known error shapes:** `401 {"error": "Bad API key"}`, `400 {"error": "Incorrect User-Agent"}`.

**Pagination:** `?page=` (**0-indexed**, default 0) and `?per_page=` (1–100, default 20).
Always request 100. The 0-index is an off-by-one trap — most APIs start at 1.

**Async operations:** some endpoints return a token instead of a result; poll
`/requests/{token}.json` for `pending | running | success | failure`. Needed for bulk
operations, and worth supporting in the client even though this pipeline is read-only.

---

## 2. Credentials

Already scaffolded in this commit:

- `.gitignore` — ignores `.env`, `.env.*`, `*.pem`, `*.key`, `secrets/`
- `.env.example` — documents the three variables, no real values
- `.env` — holds the live key locally, mode `600`, **untracked** (verified with `git check-ignore`)

Config is read via `pydantic-settings` so the key is never hardcoded and never logged.

> **Two caveats on the key you supplied.**
> 1. It could not be validated — egress is blocked, so no request has been made with it.
> 2. It was pasted into a chat transcript, and `.env` lives in an **ephemeral container**
>    that is reclaimed when this session ends. For anything durable, set it as an
>    environment variable in the environment settings or as a GitHub Actions secret —
>    and rotate it in Simplero, since a transcript is not a secret store.

---

## 3. Resources to extract

Confirmed to exist in v1; v2 paths and schemas need verification against the spec.

**Tier 1 — core reporting spine**

| Resource | v1 path | Why |
|---|---|---|
| Contacts | `/customers.json` | The central entity; everything joins to it |
| Purchases | `/purchases.json`, `/purchases/search.json` | Revenue facts |
| Products | `/products.json` | Purchase dimension |
| Invoices | `/invoices.json` | Billing/revenue reconciliation |
| Tags & taggings | `/tags.json` | Primary segmentation dimension |
| Custom fields | `/account/fields.json` | **Fetch first** — defines contact schema |

**Tier 2 — marketing analytics**

Lists (`/lists.json`), subscriptions (`/subscriptions.json`), broadcasts +
`/broadcasts/{id}/activity.json`, per-contact `/customers/{id}/email_activity.json`,
automations, segments, affiliates, point types.

`email_activity` is **per-contact** — one request per contact. At any real contact count
that dominates the run. Treat it as opt-in, incremental-only, and never part of the
default full refresh.

---

## 4. Extraction strategy

Two modes, selected per resource:

**Full snapshot** — page through everything, write a timestamped partition. Correct and
simple; fine for small dimension tables (products, tags, lists, custom fields).

**Incremental** — only records changed since the last successful run, tracked by a
watermark in `state.json` (later a `_sync_state` table). Required for contacts,
purchases, and invoices as they grow.

Incremental depends on the API exposing an `updated_since`-style filter or a stable
`updated_at` sort — **unknown until the spec is readable**. Contingency if absent:

1. Sort by `id` descending and stop at the last-seen max id — catches inserts, misses updates.
2. Full snapshot on a slower cadence (nightly), diffed downstream.

Option 2 is the safe default until proven otherwise. Watermarks advance **only** after a
resource completes fully, so a mid-run failure re-reads rather than silently skipping.

---

## 5. Architecture

```
tori/
  config.py            # pydantic-settings; loads .env, validates key present
  simplero/
    client.py          # transport: auth, UA, retry/backoff, pagination iterator
    models.py          # generated from openapi.json (datamodel-code-generator)
    resources.py       # one typed accessor per resource
  extract/             # per-resource extractors -> iterator of dicts
  load/                # writers: JSONL/Parquet -> local or S3; DuckDB for query
  state.py             # watermark read/write
  cli.py               # Typer: tori sync [--resource X] [--full] [--since ...]
tests/
docs/PLAN.md
```

**Layering:** `client` knows HTTP and nothing about reporting; `extract` knows resources
and nothing about storage; `load` knows storage and nothing about Simplero. That keeps
the blast radius small when v2 schemas turn out to differ from the v1 assumptions above.

**Client behaviour**

- `httpx` with a shared client, connection pooling, explicit connect/read timeouts
- UA and auth injected once at construction
- Retry on 429 and 5xx with exponential backoff + jitter; honour `Retry-After` when present
- **Never retry 4xx other than 429** — a 401 is a bad key, not a transient fault
- Pagination as a generator so memory stays flat regardless of table size
- Structured logging of method, path, status, attempt — **never** headers, params, or bodies

**Storage:** land raw JSONL/Parquet append-only, partitioned by resource and run
timestamp, then transform. ELT rather than ETL — reprocessing a modelling bug shouldn't
mean re-hitting the API. DuckDB over the Parquet files is enough to answer reporting
questions without standing up a warehouse; swappable for BigQuery/Snowflake later.

---

## 6. Rate limiting

Simplero documents no limits, which means the ceiling is discovered in production.
Defaults, deliberately conservative:

- Sequential requests per resource; **concurrency 1** until a limit is observed
- Client-side throttle (~5 req/s), tunable by env var
- On 429: honour `Retry-After`, else exponential backoff from 1s, cap 60s, max 5 attempts
- Log every 429 loudly — the first one tells us the real limit
- Full-refresh runs must be resumable, so hitting a wall costs minutes, not the whole run

---

## 7. Privacy

Contacts, email activity, and invoices are **personal data** (GDPR applies — Simplero is
EU-facing and the account may hold EU subjects).

- Never log record payloads; log counts and ids only
- Encrypt at rest wherever extracts land; `data/` is gitignored
- Set an explicit retention window on raw partitions rather than accumulating forever
- Keep an allowlist of extracted fields — don't blindly persist every field v2 returns
- Support deletion propagation: a contact erased in Simplero must be erasable downstream

---

## 8. Testing

- `respx` to mock httpx; fixtures captured from real responses with the key **redacted**
- Unit tests: pagination termination, 0-index correctness, retry/backoff, watermarks
- Regression test asserting the UA header is present on every outbound request
- Contract test validating fixtures against `openapi.json` so schema drift fails CI
- One opt-in live smoke test (`--live`, skipped by default) hitting a cheap endpoint

---

## 9. Milestones

| # | Deliverable | Depends on |
|---|---|---|
| 1 | Egress unblocked; `openapi.json` committed; §3/§4 assumptions verified and this doc corrected | **allowlist** |
| 2 | `client.py` + generated models; authenticated call proves key works end to end | 1 |
| 3 | Tier-1 extractors + JSONL/Parquet writer + `tori sync --full` | 2 |
| 4 | Watermarks and incremental mode | 3, R1 resolved |
| 5 | DuckDB layer + first reporting views (revenue by product, contact growth, tag cohorts) | 3 |
| 6 | Scheduled runs (GitHub Actions/cron), failure alerting, run metrics | 4 |
| 7 | Tier-2 resources, `email_activity` last | 6 |

Milestone 1 is the gate. Everything downstream rests on v2 schema assumptions that are
currently unverified.

---

## 10. Risks

| ID | Risk | Mitigation |
|---|---|---|
| R1 | No incremental filter in v2 → full refresh forever | Full snapshot nightly + downstream diffing; revisit after §4 verification |
| R2 | Undocumented rate limits hit mid-run | Conservative throttle, resumable runs, alert on first 429 |
| R3 | v2 schemas differ materially from v1 assumptions | Generate models from the spec; contract tests fail loudly on drift |
| R4 | `email_activity` fan-out makes runs unbounded | Opt-in, incremental-only, never in default refresh |
| R5 | Key leaked via transcript / ephemeral container | Rotate the key; move to managed secret storage |
| R6 | v1 deprecation removes a resource with no v2 equivalent | Inventory gaps during Milestone 1 |

---

## Sources

- [Simplero/Simplero-API](https://github.com/Simplero/Simplero-API) — public API documentation
- `https://simplero.com/api/v2/docs/openapi.json` — v2 spec (**not yet retrievable**)
