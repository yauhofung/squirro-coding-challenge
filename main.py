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
            only documents published after it are returned.
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
        """Perform one API call, retrying on rate limits and server errors."""
        if self.session is None:
            # Allow usage without an explicit connect() call.
            self.session = requests.Session()
        for attempt in range(1, MAX_RETRIES + 1):
            response = self.session.get(
                API_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT_SECONDS
            )
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
            return response.json().get("response") or {}
        raise RuntimeError("NYT API request failed after %d attempts." % MAX_RETRIES)

    def _fetch_page(self, page: int, begin_date: str | None = None) -> dict[str, Any]:
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
        log.debug("Fetching page %d", page)
        return self._request(params)

    def _iter_docs(self) -> Iterator[dict[str, Any]]:
        """Yield raw article documents, transparently paging through the API."""
        since = self._parse_datetime(self.max_inc_value) if self.inc_column else None
        begin_date = since.strftime("%Y%m%d") if since is not None else None
        for page in range(MAX_PAGE + 1):
            response = self._fetch_page(page, begin_date)
            docs = response.get("docs") or []
            for doc in docs:
                if since is not None:
                    pub_date = self._parse_datetime(doc.get("pub_date"))
                    if pub_date is not None and pub_date <= since:
                        # Results are sorted newest-first, so everything from
                        # here on was already loaded in a previous run.
                        return
                yield doc
            if len(docs) < PAGE_SIZE:
                break
            meta = response.get("meta") or {}
            hits = meta.get("hits")
            if isinstance(hits, int) and (page + 1) * PAGE_SIZE >= hits:
                break

    def _update_checkpoint(self, pub_date: Any) -> None:
        """Remember the newest pub_date seen, to resume incremental loads."""
        if not pub_date:
            return
        new = self._parse_datetime(pub_date)
        current = self._parse_datetime(self.max_inc_value)
        if new is not None and (current is None or new > current):
            self.max_inc_value = pub_date

    def getDataBatch(self, batch_size: int) -> Iterator[list[dict[str, Any]]]:
        """
        Generator - Get data from source on batches.

        :returns One list for each batch. Each of those is a list of
                 dictionaries with the defined rows.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1, got %r" % batch_size)
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
