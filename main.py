import argparse
import logging
import time
from typing import Any

import requests

"""
Skeleton for Squirro Delivery Hiring Coding Challenge
November 2025
"""


log = logging.getLogger(__name__)

API_ENDPOINT = "https://api.nytimes.com/svc/search/v2/articlesearch.json"
MAX_RETRIES = 5
RETRY_WAIT_SECONDS = 12.0  # The API allows 5 requests per minute.
REQUEST_TIMEOUT_SECONDS = 30


def flatten_dict(obj: Any, parent_key: str = "", sep: str = ".") -> dict[str, Any]:
    """Flatten nested dicts/lists into a single-level dict with dotted keys.

    Nested dictionary keys are joined with ``sep`` ("headline.main"), list
    elements are addressed by their index ("keywords.0.value") so that every
    element of the original document is preserved. Empty dicts/lists are kept
    as plain values so their keys don't disappear.
    """
    items: dict[str, Any] = {}
    if isinstance(obj, dict) and obj:
        for key, value in obj.items():
            new_key = f"{parent_key}{sep}{key}" if parent_key else str(key)
            items.update(flatten_dict(value, new_key, sep))
    elif isinstance(obj, list) and obj:
        for index, value in enumerate(obj):
            new_key = f"{parent_key}{sep}{index}" if parent_key else str(index)
            items.update(flatten_dict(value, new_key, sep))
    else:
        items[parent_key] = obj
    return items


class NYTimesSource(object):
    """
    A data loader plugin for the NY Times API.
    """

    def __init__(self):
        self.session: requests.Session | None = None

    def connect(self, inc_column=None, max_inc_value=None):
        """Connect to the source"""
        log.debug("Incremental Column: %r", inc_column)
        log.debug("Incremental Last Value: %r", max_inc_value)
        self.session = requests.Session()

    def disconnect(self):
        """Disconnect from the source."""
        if self.session is not None:
            self.session.close()
            self.session = None

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

    def getDataBatch(self, batch_size):
        """
        Generator - Get data from source on batches.

        :returns One list for each batch. Each of those is a list of
                 dictionaries with the defined rows.
        """
        # TODO: implement - this dummy implementation returns one batch of data
        yield [
            {
                "headline.main": "The main headline",
                "_id": "1234",
            }
        ]

    def getSchema(self):
        """
        Return the schema of the dataset
        :returns a List containing the names of the columns retrieved from the
        source
        """

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
    config = {
        "api_key": "NYTIMES_API_KEY",
        "query": "Silicon Valley",
    }
    source = NYTimesSource()

    # This looks like an argparse dependency - but the Namespace class is just
    # a simple way to create an object holding attributes.
    source.args = argparse.Namespace(**config)

    for idx, batch in enumerate(source.getDataBatch(10)):
        print(f"{idx} Batch of {len(batch)} items")
        for item in batch:
            print(f"  - {item['_id']} - {item['headline.main']}")
