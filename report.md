# Report: `main.py` — NY Times Article Search Data Loader

## Executive summary

`main.py` (441 lines) is a self-contained, production-quality data loader for the NY Times Article Search API, written as a solution to Squirro's Delivery coding challenge (Challenge A). It streams search results in consumer-defined batch sizes, flattens nested JSON documents into single-level dictionaries with dotted keys, and supports incremental loading keyed on `pub_date`. The code is fully type-hinted, has no dependencies beyond `requests`, and is backed by an offline suite of **95 tests, all currently passing**. The two operational risks flagged in the previous revision of this report have been engineered out: the incremental checkpoint is now committed only after a run is fully consumed (an interrupted run re-fetches instead of losing articles), and the API's ~1,000-result page cap is transparently worked around with date-windowed continuation. Delivery semantics are explicitly at-least-once, with in-run de-duplication by `_id`.

## Purpose and context

The repository implements a "data loader plugin" per the challenge skeleton: a class exposing `connect()` / `disconnect()`, `getDataBatch(batch_size)`, and `getSchema()` (the camelCase names and the `argparse.Namespace` config object are retained from the provided skeleton). The runtime environment is pinned via `mise.toml` to Python 3.14 (the code itself needs only ≥ 3.10), with `requests` as the sole runtime dependency and `pytest` for development. The API key is supplied through the `NYTIMES_API_KEY` environment variable, loaded from a gitignored `.env`.

## Anatomy

| Component                 | Location      | Role                                                          |
| ------------------------- | ------------- | ------------------------------------------------------------- |
| Constants                 | `main.py:34`  | API endpoint, page size (10), page cap (100), retry policy    |
| `STATIC_FALLBACK_SCHEMA`  | `main.py:44`  | Realistic flattened NYT column names (last-resort schema)     |
| `flatten_dict()`          | `main.py:64`  | Recursive dict/list flattener, dotted keys, collision-safe    |
| `NYTimesSource.connect()` | `main.py:119` | Validates incremental config, opens a `requests.Session`      |
| `_parse_datetime()`       | `main.py:154` | Tolerant ISO-8601 parsing (handles NYT's `+0000` offsets)     |
| `_request()`              | `main.py:171` | One API call with retry, auth, and error classification       |
| `_fetch_page()`           | `main.py:259` | Builds query params (`sort=newest`, `begin_date`, `end_date`) |
| `_iter_docs()`            | `main.py:285` | Pagination generator: early-stop, de-dup, date windowing      |
| `_update_checkpoint()`    | `main.py:345` | Advances the run's _candidate_ checkpoint                     |
| `getDataBatch()`          | `main.py:359` | Re-chunks the stream into batches; commits the checkpoint     |
| `getSchema()`             | `main.py:390` | Dynamic schema with two fallback layers                       |
| Demo                      | `main.py:417` | Prints 3 batches of 10 for "Silicon Valley", then the schema  |

## How data flows

`getDataBatch()` pulls raw documents from `_iter_docs()`, which pages through the API 10 documents at a time. Each document is flattened, its keys are merged into the running schema set, the run's candidate checkpoint is updated, and documents accumulate into a list that is yielded whenever it reaches `batch_size` (plus a final partial batch). Because batching happens on the flattened stream, the caller's batch size is fully decoupled from the API's fixed page size — a batch of 25 spans three API pages transparently. Only after the final batch has been delivered is the candidate checkpoint committed to `max_inc_value`.

Pagination ends on a short/empty page or when `meta.hits` says the result set is exhausted. Hitting the API's hard cap of page 100 (~1,000 results) no longer ends the run: the loader opens a new date window (page 0 with `end_date` set to the oldest publication day seen) and keeps going until the results genuinely run out, de-duplicating the boundary-day overlap by `_id`.

## Design decisions worth noting

**Flattening is hand-rolled, lossless, and collision-safe.** `flatten_dict()` joins nested dict keys with dots (`headline.main`) and addresses list elements by index (`keywords.0.value`), so no element of a document is dropped. Empty dicts/lists are kept as values so their keys don't silently vanish, non-string keys are stringified, and if two distinct source paths ever flatten to the same key (a literal `"a.b"` next to `{"a": {"b": …}}` — theoretical for NYT documents), the later value is preserved under a `__2`-style suffix with a warning instead of silently overwriting.

**The incremental checkpoint is transactional.** During a run, `_update_checkpoint()` only advances a private candidate value; `getDataBatch()` promotes it to `max_inc_value` after the stream has been fully consumed and delivered (`main.py:388`). A run that fails or is abandoned midway leaves the checkpoint untouched, so the next run re-fetches what was missed. This makes `max_inc_value` always safe to persist and the loader's delivery semantics explicitly at-least-once.

**Incremental loading uses a two-layer, inclusive cut-off.** The query is narrowed server-side with `begin_date` (day granularity only), and the exact timestamp comparison happens client-side in `_iter_docs()`: because results are requested with `sort=newest`, the first document _strictly older_ than the checkpoint proves everything after it is already loaded, so iteration stops immediately. Documents published exactly at the checkpoint are deliberately re-yielded so that a _different_ article from the same second can never be lost; consumers de-duplicate across runs by `_id`. Documents with missing or unparseable `pub_date` are yielded rather than dropped, but never advance the checkpoint.

**The page cap is windowed around.** When a query has more results than the ~1,000 the API will serve, `_iter_docs()` restarts at page 0 with `end_date` bound to the oldest publication day seen, repeatedly, until the result set is exhausted or the incremental cut-off is reached. In-run `_id` de-duplication absorbs the day-granularity overlap between windows. The only unreachable case left — more matching articles in a _single day_ than the page cap — is detected and loudly warned about rather than looping forever.

**The checkpoint preserves the API's own string format.** `_update_checkpoint()` compares parsed datetimes but stores the raw `pub_date` string, so the value round-trips cleanly into the next `connect(max_inc_value=...)` call.

**Failures are classified, not treated uniformly.** Authentication failures (401/403) raise immediately with a helpful message; rate limits (429), server errors (5xx), network-level errors, and 200 responses with malformed JSON bodies are all retried up to 5 times, honoring the `Retry-After` header when present (clamped to [0, 120] s so a bogus header can't stall a run) and defaulting to 12 s (matching the 5 requests/minute limit); other client errors raise with a response-body excerpt.

**The API key is kept out of error messages.** Instead of `response.raise_for_status()` — whose message embeds the full request URL, including `api-key` — errors are constructed manually. Network errors get the same treatment: `requests` exception messages embed the full URL, so the raised error names only the exception class and suppresses the exception chain (`from None`). A regression test asserts the key cannot surface.

**Schema degrades gracefully.** `getSchema()` returns the sorted union of every flattened key observed so far; if nothing has been loaded it fetches one sample page (a documented, single network call); only if that fails does it fall back to `STATIC_FALLBACK_SCHEMA`, which now lists realistic flattened NYT field names (`_id`, `headline.main`, `pub_date`, …) rather than the skeleton's placeholder columns.

## Remaining limitations

The previous revision listed six risks; all have been mitigated in code or reduced to documented, deliberate trade-offs. What genuinely remains:

1. **Boundary documents duplicate across runs — by design.** The inclusive cut-off re-yields articles published exactly at the checkpoint, and an interrupted run re-fetches everything it didn't finish. This is the safe half of the at-least-once trade-off; downstream consumers must upsert/de-duplicate by `_id` (stated in the README).
2. **A single day exceeding the page cap is unreachable.** `end_date` has only day granularity, so >~1,000 matching articles published on one calendar day cannot be windowed past. The loader detects this, warns, and moves on. NYT publishes roughly 250 articles a day in total, so this is a theoretical bound, not a practical one.
3. **Very large catch-ups are bounded by the API quota, not the loader.** Date windowing will happily issue hundreds of requests; the API's 500 requests/day quota is the real ceiling. If it is exhausted mid-run the loader raises after its retries — and because the checkpoint was never committed, resuming the next day is lossless.
4. **Retries block the thread** (`time.sleep`) — acceptable for a batch loader, now with the total wait bounded by the Retry-After cap.
5. **In-run de-duplication holds all seen `_id`s in memory** — proportional to the run size, negligible for NYT volumes.

## Verification

The offline suite (`test_main.py`, **95 tests, all passing**, across 8 test classes) replaces `requests.Session` with a fake that replays canned API responses — or raises canned network errors — covering the flattener (including collision handling), timestamp parsing, connect/disconnect, retry behavior (rate limits, Retry-After clamping, server and network errors, malformed JSON bodies, key-leak prevention), all pagination stop conditions plus date-windowed continuation and its stuck-window guard, de-duplication, batching, checkpoint commit/abandon/failure semantics, incremental loading (including an end-to-end incremental re-run with a boundary document), and schema derivation. A Snyk Code scan of the repository after these changes reports **0 issues**.

## Overall assessment

This is a strong, well-scoped solution: small surface area, clear separation between the pure flattener and the I/O-bound loader, and test coverage that exercises the tricky paths (retries, pagination edges, windowing, checkpoint semantics) rather than just the happy path. With commit-on-completion checkpoints, date-windowed catch-up, and explicit at-least-once delivery, the two caveats that previously stood between this code and use as a real ingestion component have been addressed; what remains are documented trade-offs inherent to the API itself.

---

_Label: This report was largely generated by AI (Claude Code) from a review of the repository._
