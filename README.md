# Squirro Delivery Coding Challenge — NY Times Article Search Loader

A data loader plugin (Challenge A) that fetches news articles from the
[NY Times Article Search API](https://developer.nytimes.com/docs/articlesearch-product/1/overview)
and yields them in batches as flattened Python dictionaries
(`headline.main`, `keywords.0.value`, …).

## Setup

1. Get a free API key at [developer.nytimes.com](https://developer.nytimes.com/):
   create an account, register an app, and enable the **Article Search API**.
2. Install the dependency (a virtualenv is recommended):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Provide the API key via the environment (never hardcoded):

   ```bash
   export NYTIMES_API_KEY="your-key-here"
   ```

   Alternatively put `NYTIMES_API_KEY=your-key-here` into a `.env` file
   (already gitignored) and source it: `set -a; . ./.env; set +a`.

### With mise (alternative)

If you use [mise](https://mise.jdx.dev/), `mise.toml` handles all of the
above — it pins Python/uv, creates the virtualenv, installs dependencies
with [uv](https://docs.astral.sh/uv/), and auto-loads `.env`:

```bash
mise trust
MISE_PYTHON_PRECOMPILED_FLAVOR=install_only mise install   # once
mise run run     # install deps (via uv) and run the demo
mise run update  # upgrade deps to newest allowed versions
```

The flavor override works around mise (as of 2026.3.1) picking a broken
free-threaded "stripped" build for Python 3.13+; it is harmless once that
is fixed upstream.

## Run

```bash
python3 main.py
```

The demo searches for “Silicon Valley”, prints three batches of 10 articles
(`_id` + `headline.main`), then prints the dynamically derived schema.

## How it works

- **`getDataBatch(batch_size)`** is a generator that transparently pages
  through the API (10 docs per page) and re-chunks the stream into batches of
  the requested size, so `batch_size` is independent of the API page size.
  Pagination stops when a page comes back short/empty, when `meta.hits` is
  exhausted, or at the API's page cap (100).
- **`flatten_dict()`** is a hand-written recursive flattener (no third-party
  library, per the challenge). Nested dicts use dot notation
  (`headline.main`), list elements keep their index (`keywords.0.value`) so
  _all_ elements of each document are preserved, and empty dicts/lists are
  kept as values so no key silently disappears.
- **Rate limiting:** the API allows 5 requests/minute and 500/day. On
  HTTP 429 (or 5xx) the loader waits — honouring `Retry-After` when present,
  12 s otherwise — and retries up to 5 times before giving up.

## Bonus features

- **Incremental loading:** pass the incremental column and the last loaded
  value to `connect()`:

  ```python
  source.connect(inc_column="pub_date", max_inc_value="2026-07-01T00:00:00+0000")
  ```

  The loader narrows the query server-side with `begin_date`, sorts results
  newest-first, and stops as soon as it reaches an article published at or
  before `max_inc_value`. While loading, `source.max_inc_value` is advanced
  to the newest `pub_date` seen, so it can be persisted and passed back on
  the next run.

- **Dynamic schema:** `getSchema()` returns the sorted union of all flattened
  keys observed while loading; if nothing has been loaded yet it derives the
  schema from a sample API page, and only falls back to a static column list
  if the API is unreachable.

## Assumptions

- Only `pub_date` is supported as the incremental column — it is the only
  natural cursor the Article Search API exposes; `connect()` rejects others.
- `max_inc_value` is an ISO-8601 timestamp (as returned by the API itself).
  Naive timestamps are interpreted as UTC.
- The incremental cut-off is exclusive: documents with
  `pub_date == max_inc_value` are considered already loaded.
- The demo in `__main__` stops after 3 batches to stay inside the
  5 requests/minute rate limit; the loader itself streams all available
  results (the API serves at most ~1,000 per query).
- The flattened schema varies per document (e.g. number of keywords), which
  is why the dynamic schema is the union of keys across observed documents.

## Transparency note

This solution was developed with AI assistance (Anthropic's Claude Code),
in line with the challenge's guideline on helper tools; the design decisions,
review, and testing were done by the author.
