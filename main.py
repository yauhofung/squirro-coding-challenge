"""NY Times Article Search data loader.

Implements :class:`NYTimesSource`, a data loader plugin that streams articles
from the NY Times Article Search API in batches. Nested API documents are
flattened into single-level dictionaries with dotted keys (e.g.
"headline.main"), and incremental loading via ``pub_date`` ensures repeated
runs only return newly published articles. Rate limits and transient server
errors are retried automatically.

Run as a script for a short demo (requires the ``NYTIMES_API_KEY``
environment variable):

    python main.py
"""

import argparse
import itertools
import logging
import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

API_ENDPOINT = "https://api.nytimes.com/svc/search/v2/articlesearch.json"
PAGE_SIZE = 10  # The Article Search API always returns 10 docs per page.
MAX_PAGE = 100  # The API rejects page values above 100 (~1,000 results max).
MAX_RETRIES = 5
RETRY_WAIT_SECONDS = 12.0  # The API allows 5 requests per minute.
MAX_RETRY_WAIT_SECONDS = 120.0  # Cap Retry-After so a bogus header can't stall a run.
REQUEST_TIMEOUT_SECONDS = 30


def flatten_dict(obj: Any, parent_key: str = "", sep: str = ".") -> dict[str, Any]:
    """Flatten nested dicts/lists into a single-level dict with dotted keys.

    Nested dictionary keys are joined with ``sep`` ("headline.main"), list
    elements are addressed by their index ("keywords.0.value") so that every
    element of the original document is preserved. Empty dicts/lists are kept
    as plain values so their keys don't disappear. Distinct source paths that
    flatten to the same key (e.g. a literal "a.b" key next to
    ``{"a": {"b": ...}}``) are kept under a "__2"-style suffix instead of
    silently overwriting one another.
    """
    flat: dict[str, Any] = {}
    _flatten_into(obj, parent_key, sep, flat)
    return flat


def _flatten_into(obj: Any, key: str, sep: str, out: dict[str, Any]) -> None:
    """Recursive helper for :func:`flatten_dict`, accumulating into ``out``."""
    if isinstance(obj, dict) and obj:
        for child_key, value in obj.items():
            _flatten_into(
                value, f"{key}{sep}{child_key}" if key else str(child_key), sep, out
            )
    elif isinstance(obj, list) and obj:
        for index, value in enumerate(obj):
            _flatten_into(value, f"{key}{sep}{index}" if key else str(index), sep, out)
    else:
        if key in out:
            suffix = 2
            while f"{key}__{suffix}" in out:
                suffix += 1
            log.warning(
                "Flattened key collision on %r; keeping the extra value as %r",
                key,
                f"{key}__{suffix}",
            )
            key = f"{key}__{suffix}"
        out[key] = obj


class NYTimesSource(object):
    """
    A data loader plugin for the NY Times API.
    """

    # Populated externally with the loader configuration (see __main__).
    args: argparse.Namespace

    def __init__(self):
        self.session: requests.Session | None = None
        self.inc_column: str | None = None
        self.max_inc_value: str | None = None
        self._candidate_inc_value: str | None = None
        self._seen_keys: set[str] = set()

    def connect(
        self,
        inc_column: str | None = None,
        max_inc_value: str | None = None,
    ) -> None:
        """Connect to the source.

        :param inc_column: Column used for incremental loading. Only
            "pub_date" is supported for the Article Search API.
        :param max_inc_value: Highest ``inc_column`` value already loaded;
            only documents published at or after it are returned. Documents
            published exactly at ``max_inc_value`` are returned again so that
            a different article from the same second is never lost -
            de-duplicate across runs by ``_id`` downstream. The attribute is
            advanced only after a ``getDataBatch()`` run has been fully
            consumed, so it is always safe to persist.
        """
        log.debug("Incremental Column: %r", inc_column)
        log.debug("Incremental Last Value: %r", max_inc_value)
        if inc_column and inc_column != "pub_date":
            raise ValueError(
                "Only 'pub_date' is supported as incremental column, got %r"
                % inc_column
            )
        self.inc_column = inc_column
        self.max_inc_value = max_inc_value
        self.session = requests.Session()

    def disconnect(self):
        """Disconnect from the source."""
        if self.session is not None:
            self.session.close()
            self.session = None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        """Parse an ISO-8601 timestamp such as NYT's "2026-07-21T12:34:56+0000"."""
        if value in (None, ""):
            return None
        text = str(value).strip().replace("Z", "+00:00")
        # Normalize "+0000" style offsets (no colon) for older Pythons.
        if len(text) >= 5 and text[-5] in "+-" and text[-4:].isdigit():
            text = f"{text[:-2]}:{text[-2:]}"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            log.warning("Could not parse timestamp %r", value)
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Perform one API call, retrying transient failures.

        Retried (up to MAX_RETRIES attempts): HTTP 429 and 5xx - honouring
        ``Retry-After`` capped at MAX_RETRY_WAIT_SECONDS - network errors,
        and 200 responses whose body is not a JSON object. Fatal: HTTP
        401/403 (bad key) and other client errors.
        """
        if self.session is None:
            # Allow usage without an explicit connect() call.
            self.session = requests.Session()
        for attempt in range(1, MAX_RETRIES + 1):
            wait = RETRY_WAIT_SECONDS
            try:
                response = self.session.get(
                    API_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT_SECONDS
                )
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    # Only the exception class is reported: requests error
                    # messages embed the full URL, api-key included, so the
                    # original exception must not surface (hence "from None").
                    raise RuntimeError(
                        "NYT API request kept failing with network errors (%s) after %d attempts."
                        % (type(exc).__name__, MAX_RETRIES)
                    ) from None
                log.warning(
                    "Network error (%s) talking to the NYT API, retrying in %.0fs (attempt %d/%d)",
                    type(exc).__name__,
                    wait,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            if response.status_code in (401, 403):
                raise RuntimeError(
                    "NYT API authentication failed (HTTP %s). Set a valid key with Article Search access, e.g. via the NYTIMES_API_KEY environment variable."
                    % response.status_code
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        "NYT API request kept failing with HTTP %s after %d attempts."
                        % (response.status_code, MAX_RETRIES)
                    )
                try:
                    wait = float(response.headers["Retry-After"])
                except (KeyError, ValueError):
                    wait = RETRY_WAIT_SECONDS
                wait = min(max(wait, 0.0), MAX_RETRY_WAIT_SECONDS)
                log.warning(
                    "HTTP %s from NYT API, retrying in %.0fs (attempt %d/%d)",
                    response.status_code,
                    wait,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            if not response.ok:
                # Build the error ourselves so the api-key never leaks into
                # logs as part of the requested URL.
                raise RuntimeError(
                    "NYT API request failed with HTTP %s: %s"
                    % (response.status_code, response.text[:200])
                )
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        "NYT API returned a malformed JSON body (HTTP 200) after %d attempts."
                        % MAX_RETRIES
                    )
                log.warning(
                    "Malformed JSON body from the NYT API, retrying in %.0fs (attempt %d/%d)",
                    wait,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            return payload.get("response") or {}
        raise RuntimeError("NYT API request failed after %d attempts." % MAX_RETRIES)

    def _fetch_page(
        self,
        page: int,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one page of Article Search results (10 docs per page)."""
        params = {
            "q": self.args.query,
            "api-key": self.args.api_key,
            "page": page,
            # Stable newest-first order enables the incremental cut-off.
            "sort": "newest",
        }
        if begin_date:
            # Server-side narrowing for incremental runs; begin_date only has
            # day granularity, the exact cut-off is applied in _iter_docs().
            params["begin_date"] = begin_date
        if end_date:
            # Day-granular upper bound used to continue past the page cap.
            params["end_date"] = end_date
        log.debug(
            "Fetching page %d (begin_date=%s, end_date=%s)", page, begin_date, end_date
        )
        return self._request(params)

    def _iter_docs(self) -> Iterator[dict[str, Any]]:
        """Yield raw article documents, transparently paging through the API.

        Results are requested newest-first. Incremental runs stop at the
        first document *strictly older* than the checkpoint; documents
        published exactly at the checkpoint are yielded again so same-second
        articles are never lost. Documents are de-duplicated by ``_id``
        within the run, and when a query has more results than the API's
        page cap can serve, iteration continues in successively older
        ``end_date`` windows until the results are exhausted.
        """
        since = self._parse_datetime(self.max_inc_value) if self.inc_column else None
        begin_date = since.strftime("%Y%m%d") if since is not None else None
        seen_ids: set[str] = set()
        end_date: str | None = None
        while True:
            oldest: datetime | None = None
            for page in range(MAX_PAGE + 1):
                response = self._fetch_page(page, begin_date, end_date)
                docs = response.get("docs") or []
                for doc in docs:
                    pub_date = self._parse_datetime(doc.get("pub_date"))
                    if pub_date is not None and (oldest is None or pub_date < oldest):
                        oldest = pub_date
                    if since is not None and pub_date is not None and pub_date < since:
                        # Results are sorted newest-first, so everything from
                        # here on was already loaded in a previous run.
                        return
                    doc_id = doc.get("_id")
                    if doc_id is not None:
                        if doc_id in seen_ids:
                            # Repeat caused by results shifting mid-run or by
                            # date-window overlap - already yielded once.
                            continue
                        seen_ids.add(doc_id)
                    yield doc
                if len(docs) < PAGE_SIZE:
                    return
                meta = response.get("meta") or {}
                hits = meta.get("hits")
                if isinstance(hits, int) and (page + 1) * PAGE_SIZE >= hits:
                    return
            # The page cap was reached with results still remaining: continue
            # in an older window, bounded by the oldest publication day seen.
            if oldest is None:
                log.warning(
                    "Page cap reached but no parseable pub_date seen; cannot window further, stopping with results remaining."
                )
                return
            next_end = oldest.strftime("%Y%m%d")
            if end_date is not None and next_end >= end_date:
                # end_date only has day granularity: a single day holding
                # more results than the page cap cannot be windowed past.
                log.warning(
                    "More results than the API page cap within %s; older matching articles cannot be reached.",
                    next_end,
                )
                return
            end_date = next_end

    def _update_checkpoint(self, pub_date: Any) -> None:
        """Track the newest pub_date seen by the current run.

        Only the candidate checkpoint is advanced here; getDataBatch()
        promotes it to ``max_inc_value`` once the run has been fully
        consumed, so an interrupted run never skips undelivered articles.
        """
        if not pub_date:
            return
        new = self._parse_datetime(pub_date)
        current = self._parse_datetime(self._candidate_inc_value)
        if new is not None and (current is None or new > current):
            self._candidate_inc_value = pub_date

    def getDataBatch(self, batch_size: int) -> Iterator[list[dict[str, Any]]]:
        """
        Generator - Get data from source on batches.

        The incremental checkpoint (``max_inc_value``) is committed only
        after the final batch has been delivered: if the run fails or the
        generator is abandoned midway, the checkpoint keeps its previous
        value and the next run re-fetches the missed articles instead of
        skipping them (at-least-once delivery).

        :returns One list for each batch. Each of those is a list of
                 dictionaries with the defined rows.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1, got %r" % batch_size)
        self._candidate_inc_value = self.max_inc_value
        batch: list[dict[str, Any]] = []
        for doc in self._iter_docs():
            flat = flatten_dict(doc)
            self._seen_keys.update(flat)
            self._update_checkpoint(flat.get("pub_date"))
            batch.append(flat)
            if len(batch) == batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
        # Every document has been delivered - only now is it safe to commit
        # the checkpoint for the next incremental run.
        self.max_inc_value = self._candidate_inc_value

    def getSchema(self) -> list[str]:
        """
        Return the schema of the dataset
        :returns a List containing the names of the columns retrieved from the
        source
        """
        if not self._seen_keys:
            # Nothing loaded yet - derive the schema from a sample page.
            try:
                response = self._fetch_page(0)
                for doc in response.get("docs") or []:
                    self._seen_keys.update(flatten_dict(doc))
            except Exception:
                log.warning(
                    "Could not derive a dynamic schema from the API, falling back to the static column list",
                    exc_info=True,
                )
        if self._seen_keys:
            return sorted(self._seen_keys)

        schema = [
            "title",
            "body",
            "created_at",
            "id",
            "summary",
            "abstract",
            "keywords",
        ]

        return schema


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    config = {
        # Never hardcode credentials - the key is read from the environment.
        "api_key": os.environ.get("NYTIMES_API_KEY", "NYTIMES_API_KEY"),
        "query": "Silicon Valley",
    }
    source = NYTimesSource()

    # This looks like an argparse dependency - but the Namespace class is just
    # a simple way to create an object holding attributes.
    source.args = argparse.Namespace(**config)

    source.connect()
    try:
        # The loader streams every available result; the demo stops after a
        # few batches to stay within the API rate limit (5 requests/minute).
        for idx, batch in enumerate(itertools.islice(source.getDataBatch(10), 3)):
            print(f"{idx} Batch of {len(batch)} items")
            for item in batch:
                print(f"  - {item['_id']} - {item['headline.main']}")
        print(f"Schema: {source.getSchema()}")
    finally:
        source.disconnect()
